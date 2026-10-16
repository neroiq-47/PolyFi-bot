"""Live-trading and Cloudflare-deployment safeguards."""

from types import SimpleNamespace as NS

import httpx
import pytest
from fastapi.testclient import TestClient

from polyfi import gateways, server
from polyfi.config import Settings
from polyfi.engine import Engine
from polyfi.gateways import LiveGateway, check_geoblock
from polyfi.store import Store
from tests.test_engine import StubFeed, StubGateway

LOCAL = "http://127.0.0.1:8787"
TOKEN = {"X-Polyfi-Token": "tok"}


class FakeResponse:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


def fake_geo(monkeypatch, data):
    async def get(self, url, **kw):
        return FakeResponse(data)

    monkeypatch.setattr(httpx.AsyncClient, "get", get)


async def test_geoblock_blocks_live(monkeypatch):
    fake_geo(monkeypatch, {"blocked": True, "country": "US", "region": "NY"})
    with pytest.raises(RuntimeError, match="US-NY"):
        await check_geoblock()


async def test_geoblock_allows_and_tolerates_outage(monkeypatch):
    fake_geo(monkeypatch, {"blocked": False, "country": "GB"})
    await check_geoblock()

    async def boom(self, url, **kw):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx.AsyncClient, "get", boom)
    await check_geoblock()  # unreachable check must not block startup


def test_failed_live_start_comes_up_in_paper_with_reason(monkeypatch):
    async def fail(settings):
        raise RuntimeError("Polymarket blocks trading from this server's location (US)")

    monkeypatch.setattr(LiveGateway, "connect", classmethod(lambda cls, s: fail(s)))
    app = server.create_app(Settings(mode="live", db_path=":memory:", autostart=True))
    with TestClient(app, base_url=LOCAL) as c:
        s = c.get("/api/state", headers={"X-Polyfi-Token": ""}).json()
        assert s["mode"] == "paper"
        assert "Live mode failed to start" in s["startup_error"] and "US" in s["startup_error"]
        assert s["running"] is False  # autostart never kicks in after a failed live start


def test_autostart_resumes_trading():
    app = server.create_app(Settings(mode="demo", db_path=":memory:", autostart=True))
    with TestClient(app, base_url=LOCAL) as c:
        assert c.get("/api/state", headers={"X-Polyfi-Token": ""}).json()["running"] is True
    app = server.create_app(Settings(mode="demo", db_path=":memory:"))
    with TestClient(app, base_url=LOCAL) as c:
        assert c.get("/api/state", headers={"X-Polyfi-Token": ""}).json()["running"] is False


class FakeLive(LiveGateway):
    name = "live"

    def __init__(self):
        self.sent = []
        self.builder_code = None
        self.session_key = False
        self._client = NS(wallet="0x" + "1" * 40)

    async def find_market(self, slug, start, end):
        return None

    async def balance(self):
        return 100.0

    async def transfer_pusd(self, to, amount):
        self.sent.append((to, amount))
        return "ok"

    async def close(self):
        pass


def live_app(monkeypatch, **kw):
    async def build(settings):
        return Engine(settings, StubFeed(), FakeLive(), Store(":memory:"))

    monkeypatch.setattr(server, "build_engine", build)

    async def bridge(gw, wallet, recipient, amount):
        await gw.transfer_pusd("0xbridge", amount)
        return {"amount": amount, "recipient": recipient}

    monkeypatch.setattr(server.funds, "withdraw_usdc_polygon", bridge)
    return server.create_app(Settings(mode="live", db_path=":memory:", dashboard_token="tok", **kw))


ME = "0x" + "a" * 40
EVIL = "0x" + "e" * 40


def test_public_dashboard_withdraw_requires_allowlist(monkeypatch):
    app = live_app(monkeypatch, host="0.0.0.0")
    with TestClient(app, base_url="https://polyfi-bot.example.workers.dev") as c:
        r = c.post("/api/withdraw", json={"amount": 5, "recipient": EVIL}, headers=TOKEN)
        assert r.status_code == 403 and "POLYFI_WITHDRAW_ADDRESS" in r.text
        assert c.app.state.engine.gw.sent == []


def test_withdraw_locked_to_allowlisted_address(monkeypatch):
    app = live_app(monkeypatch, host="0.0.0.0", withdraw_address=ME)
    with TestClient(app, base_url="https://polyfi-bot.example.workers.dev") as c:
        assert c.post("/api/withdraw", json={"amount": 5, "recipient": EVIL}, headers=TOKEN).status_code == 403
        r = c.post("/api/withdraw", json={"amount": 5, "recipient": ME.upper().replace("0X", "0x")}, headers=TOKEN)
        assert r.status_code == 200
        assert c.app.state.engine.gw.sent == [("0xbridge", 5.0)]


class ExchangePnLGateway(StubGateway):
    def __init__(self, pnl):
        super().__init__()
        self.pnl = pnl

    async def realized_pnl_today(self):
        return self.pnl


async def test_daily_loss_uses_polymarket_pnl_after_restart(tmp_path):
    # Fresh container: empty local journal, but Polymarket says we're already down $30 today.
    s = Settings(db_path=str(tmp_path / "r.db"), max_daily_loss_usd=25, entry_after_s=20, stop_before_end_s=60)
    feed, gw = StubFeed(), ExchangePnLGateway(-30.0)
    e = Engine(s, feed, gw, Store(s.db_path))
    e.running = True
    feed.price = 100.3
    await e.tick(3100)
    assert e.window.position is None and "daily loss" in e.halted_reason


async def test_realized_pnl_today_from_series(monkeypatch):
    import datetime as dt

    now = dt.datetime.now(dt.timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    pts = [NS(timestamp=midnight - dt.timedelta(hours=2), realized_pnl=10),
           NS(timestamp=midnight, realized_pnl=12),
           NS(timestamp=now, realized_pnl=4)]

    async def pnl(**kw):
        return NS(points=tuple(pts))

    gw = LiveGateway(NS(get_user_pnl=pnl))
    assert await gw.realized_pnl_today() == -8.0
