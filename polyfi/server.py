"""FastAPI app: runs the engine and serves the dashboard.

Run:  python -m polyfi            (defaults to http://127.0.0.1:8787, demo mode)
"""

from __future__ import annotations

import hmac
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse

from . import funds
from .config import Settings
from .engine import Engine
from .feeds import CoinbaseFeed, DemoFeed
from .gateways import DemoGateway, LiveGateway, PaperGateway
from .store import Store
from .streams import ChainlinkFeed

WEB = Path(__file__).parent / "web"
log = logging.getLogger("polyfi")


async def build_engine(settings: Settings) -> Engine:
    store = Store(settings.db_path)
    if settings.mode == "demo":
        feed = DemoFeed()
        gateway = DemoGateway(feed, settings)
    elif settings.mode == "paper":
        feed, gateway = CoinbaseFeed(), PaperGateway()
    elif settings.mode == "live":
        gateway = await LiveGateway.connect(settings)
        # Chainlink is what these markets resolve on; Coinbase covers gaps.
        feed = ChainlinkFeed(gateway._client, fallback=CoinbaseFeed())
        feed.start()
    else:
        raise ValueError("POLYFI_MODE must be demo, paper or live")
    return Engine(settings, feed, gateway, store)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = await build_engine(settings)
        app.state.engine = engine
        engine.start_loop()
        try:
            yield
        finally:
            await engine.stop_loop()
            await engine.gw.close()
            await engine.feed.close()

    app = FastAPI(title="PolyFi Bot", lifespan=lifespan)

    def auth(x_polyfi_token: str = Header(default="")) -> None:
        if settings.dashboard_token and not hmac.compare_digest(x_polyfi_token, settings.dashboard_token):
            raise HTTPException(401, "bad dashboard token")

    def engine(request: Request) -> Engine:
        return request.app.state.engine

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(WEB / "index.html")

    @app.get("/api/state", dependencies=[Depends(auth)])
    async def state(e: Engine = Depends(engine)) -> dict:
        return e.state()

    @app.post("/api/bot/start", dependencies=[Depends(auth)])
    async def start(e: Engine = Depends(engine)) -> dict:
        e.halted_reason = ""
        e.running = True
        e.note("bot started")
        return {"running": True}

    @app.post("/api/bot/stop", dependencies=[Depends(auth)])
    async def stop(e: Engine = Depends(engine)) -> dict:
        e.running = False
        await e._cancel_resting("paused by user")
        e.note("bot paused")
        return {"running": False}

    @app.post("/api/settings", dependencies=[Depends(auth)])
    async def update_settings(changes: dict, e: Engine = Depends(engine)) -> dict:
        try:
            e.settings.update(changes)
        except (ValueError, TypeError) as err:
            raise HTTPException(400, str(err)) from err
        e.note(f"settings updated: {', '.join(f'{k}={v}' for k, v in changes.items())}")
        return e.settings.public()

    @app.get("/api/deposit", dependencies=[Depends(auth)])
    async def deposit(e: Engine = Depends(engine)) -> dict:
        if not isinstance(e.gw, LiveGateway):
            return {"mode": e.gw.name, "wallet": None, "addresses": {}}
        try:
            addresses = await funds.deposit_addresses(e.gw.wallet)
        except Exception as err:
            log.warning("deposit address lookup failed: %s", err)
            addresses = {}
        return {"mode": "live", "wallet": e.gw.wallet, "addresses": addresses}

    @app.post("/api/withdraw", dependencies=[Depends(auth)])
    async def withdraw(body: dict, e: Engine = Depends(engine)) -> dict:
        if not isinstance(e.gw, LiveGateway):
            raise HTTPException(400, "withdrawals only work in live mode")
        try:
            result = await funds.withdraw_usdc_polygon(
                e.gw, e.gw.wallet, str(body.get("recipient", "")), float(body.get("amount", 0))
            )
        except ValueError as err:
            raise HTTPException(400, str(err)) from err
        e.note(f"withdrew ${result['amount']:.2f} USDC (Polygon) → {result['recipient'][:10]}…")
        return result

    return app
