"""FastAPI app: runs the engine and serves the dashboard.

Run:  python -m polyfi            (defaults to http://127.0.0.1:8787, demo mode)
"""

from __future__ import annotations

import hmac
import logging
import math
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from . import funds
from .config import Settings
from .engine import Engine
from .feeds import CoinbaseFeed, DemoFeed
from .gateways import DemoGateway, LiveGateway, PaperGateway
from .store import Store
from .streams import ChainlinkFeed

WEB = Path(__file__).parent / "web"
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _hostname(host: str) -> str:
    """'127.0.0.1:8787' -> '127.0.0.1', '[::1]:8787' -> '::1'."""
    if host.startswith("["):
        return host[1:].split("]", 1)[0]
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host
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

    expected_token = (settings.dashboard_token or "").encode()

    def _header(request: Request, name: bytes) -> bytes | None:
        # Raw bytes straight from the ASGI scope: no decoding, so odd values can't 500.
        for key, value in request.scope.get("headers", ()):
            if key.lower() == name:
                return value
        return None

    @app.middleware("http")
    async def guard_api(request: Request, call_next):
        """CSRF + auth for every /api/* call.

        The custom ``X-Polyfi-Token`` header is mandatory even when no token is
        configured: browsers only send custom headers cross-site after a CORS
        preflight, which this server never approves, so a malicious page can't
        drive the bot. A foreign ``Origin`` is rejected as defense in depth.
        """
        path = request.url.path
        if path == "/api" or path.startswith("/api/"):
            token = _header(request, b"x-polyfi-token")
            if token is None:
                return JSONResponse({"detail": "missing X-Polyfi-Token header"}, status_code=403)
            if expected_token:
                # Token mode (e.g. behind the Cloudflare Worker, where Host may be rewritten):
                # the secret token is the gate.
                if not hmac.compare_digest(token, expected_token):
                    return JSONResponse({"detail": "bad dashboard token"}, status_code=401)
            else:
                # No token: only a local dashboard may talk to us. Requiring a loopback Host
                # defeats DNS rebinding (a rebound evil.com page sends Host: evil.com).
                host = (_header(request, b"host") or b"").decode("latin-1").strip().lower()
                if _hostname(host) not in LOOPBACK_HOSTS:
                    return JSONResponse({"detail": "set POLYFI_DASHBOARD_TOKEN for non-local access"}, status_code=403)
                origin = _header(request, b"origin")
                if origin is not None:
                    try:
                        origin_host = urlsplit(origin.decode("latin-1").strip()).netloc.lower()
                    except ValueError:
                        origin_host = ""
                    if not origin_host or origin_host != host:
                        return JSONResponse({"detail": "cross-origin request rejected"}, status_code=403)
        return await call_next(request)

    def engine(request: Request) -> Engine:
        return request.app.state.engine

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(WEB / "index.html")

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True}

    @app.get("/api/state")
    async def state(e: Engine = Depends(engine)) -> dict:
        return e.state()

    @app.post("/api/bot/start")
    async def start(e: Engine = Depends(engine)) -> dict:
        e.halted_reason = ""
        e.running = True
        e.note("bot started")
        return {"running": True}

    @app.post("/api/bot/stop")
    async def stop(e: Engine = Depends(engine)) -> dict:
        e.running = False
        await e._cancel_resting("paused by user")
        e.note("bot paused")
        return {"running": False}

    @app.post("/api/settings")
    async def update_settings(changes: dict, e: Engine = Depends(engine)) -> dict:
        try:
            e.settings.update(changes)
        except (ValueError, TypeError) as err:
            raise HTTPException(400, str(err)) from err
        e.note(f"settings updated: {', '.join(f'{k}={v}' for k, v in changes.items())}")
        return e.settings.public()

    @app.get("/api/deposit")
    async def deposit(e: Engine = Depends(engine)) -> dict:
        if not isinstance(e.gw, LiveGateway):
            return {"mode": e.gw.name, "wallet": None, "addresses": {}}
        try:
            addresses = await funds.deposit_addresses(e.gw.wallet, e.gw.builder_code)
        except Exception as err:
            log.warning("deposit address lookup failed: %s", err)
            addresses = {}
        return {"mode": "live", "wallet": e.gw.wallet, "addresses": addresses}

    @app.post("/api/withdraw")
    async def withdraw(request: Request, e: Engine = Depends(engine)) -> dict:
        try:
            body = await request.json()
        except ValueError as err:
            raise HTTPException(400, "body must be JSON") from err
        if not isinstance(body, dict):
            raise HTTPException(400, "body must be a JSON object")
        try:
            amount = float(body.get("amount", 0))
        except (TypeError, ValueError) as err:
            raise HTTPException(400, "amount must be a number") from err
        if not math.isfinite(amount) or amount <= 0:
            raise HTTPException(400, "amount must be a positive number")
        if not isinstance(e.gw, LiveGateway):
            raise HTTPException(400, "withdrawals only work in live mode")
        try:
            result = await funds.withdraw_usdc_polygon(e.gw, e.gw.wallet, str(body.get("recipient", "")), amount)
        except ValueError as err:
            raise HTTPException(400, str(err)) from err
        e.note(f"withdrew ${result['amount']:.2f} USDC (Polygon) → {result['recipient'][:10]}…")
        return result

    return app
