"""Behaviour required by Polymarket's docs (place-orders, market-details, matching-engine, bridge)."""

import asyncio
from decimal import Decimal
from types import SimpleNamespace as NS

import pytest

from polyfi import funds
from polyfi.config import Settings
from polyfi.engine import Engine
from polyfi.gateways import Book, WindowMarket, _find_market
from polyfi.model import decide
from polyfi.store import Store
from tests.test_engine import StubFeed, StubGateway

T0 = 3000


def market(**state):
    s = {"active": True, "closed": False, "accepting_orders": True, **state}
    return NS(
        state=NS(**s), condition_id="0xc", question="BTC up or down?",
        outcomes=NS(yes=NS(label="Up", token_id="111", position_id=None), no=NS(label="Down", token_id="222", position_id=None)),
        trading=NS(minimum_tick_size=Decimal("0.01"), minimum_order_size=Decimal("5"), fees_enabled=True,
                   fee_schedule=NS(rate=Decimal("0.05"), exponent=1, taker_only=True, rebate_rate=Decimal("0.2"))),
    )


class Client:
    def __init__(self, m):
        self.m = m

    async def get_market(self, slug):
        return self.m


async def test_market_fields_follow_docs():
    wm = await _find_market(Client(market()), "s", 0, 300)
    assert (wm.up_asset, wm.down_asset, wm.fee_rate, wm.accepting_orders) == ("111", "222", 0.05, True)
    assert (await _find_market(Client(market(accepting_orders=False)), "s", 0, 300)).accepting_orders is False
    assert (await _find_market(Client(market(active=None)), "s", 0, 300)).accepting_orders is True
    assert await _find_market(Client(market(closed=True)), "s", 0, 300) is None
    m = market(); m.trading.fees_enabled = False
    assert (await _find_market(Client(m), "s", 0, 300)).fee_rate == 0.0


def test_taker_edge_uses_market_fee_rate():
    # fair 0.60 vs ask 0.55: with the crypto 0.07 rate the fee kills the edge; with no fee it trades.
    assert decide(0.60, 0.53, 0.55, 0.40, 0.47, min_edge=0.04, free_only=False).side is None
    assert decide(0.60, 0.53, 0.55, 0.40, 0.47, min_edge=0.04, free_only=False, fee_rate=0.0).side == "UP"


class RecordingGateway(StubGateway):
    def __init__(self):
        super().__init__()
        self.sizes = []
        self.market_kwargs = {}
        self.fail_with = None

    async def find_market(self, slug, start, end):
        return WindowMarket(slug, start, end, "up", "down", **self.market_kwargs)

    async def buy_maker(self, asset, price, size):
        if self.fail_with:
            raise self.fail_with
        self.sizes.append(size)
        return await super().buy_maker(asset, price, size)


@pytest.fixture
def eng(tmp_path):
    s = Settings(db_path=str(tmp_path / "d.db"), stake_usd=5, min_edge=0.04, entry_after_s=20, stop_before_end_s=60)
    feed, gw = StubFeed(), RecordingGateway()
    e = Engine(s, feed, gw, Store(s.db_path))
    e.running = True
    feed.price = 100.3
    return e, gw


async def test_share_size_rounds_down_to_two_decimals(eng):
    e, gw = eng
    e.settings.stake_usd = 6.29
    await e.tick(T0 + 100)
    assert gw.sizes == [11.23]  # rounded down, never above the stake


async def test_not_accepting_orders_blocks_entry(eng):
    e, gw = eng
    gw.market_kwargs = {"accepting_orders": False}
    await e.tick(T0 + 100)
    assert gw.sizes == [] and e.snapshot.signal["reason"] == "market not accepting orders"


async def test_restart_rejection_pauses_without_using_attempts(eng):
    e, gw = eng
    err = RuntimeError("425 too early")
    err.restriction, err.retry_after = "restarting", 3.0
    gw.fail_with = err
    await e.tick(T0 + 100)
    assert e.window.attempts == 0 and e.window.position is None
    assert "restarting" in e.snapshot.signal["reason"]
    await e.tick(T0 + 101)  # still inside the server's Retry-After
    assert "restarting" in e.snapshot.signal["reason"]
    gw.fail_with = None
    e._paused_until = 0  # Retry-After elapsed
    await e.tick(T0 + 102)
    assert gw.sizes and e.window.position is not None


async def test_withdraw_minimum_matches_bridge():
    with pytest.raises(ValueError, match=r"\$2"):
        await funds.withdraw_usdc_polygon(None, "0x" + "1" * 40, "0x" + "2" * 40, 1.5)
