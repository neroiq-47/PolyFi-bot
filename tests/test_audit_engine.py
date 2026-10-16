"""Regression tests for audited engine bugs (freeze, degenerate vol, cancel race, blocking redeem, balance polling)."""

import asyncio

import pytest

from polyfi.config import Settings
from polyfi.engine import Engine
from polyfi.gateways import Book
from polyfi.store import Store

from tests.test_engine import StubFeed, StubGateway

T0 = 3000  # a window boundary (multiple of 300)


def make(tmp_path, feed=None, gw=None):
    s = Settings(db_path=str(tmp_path / "t.db"), stake_usd=5, min_edge=0.04, entry_after_s=20, stop_before_end_s=60)
    feed, gw = feed or StubFeed(), gw or StubGateway()
    eng = Engine(s, feed, gw, Store(s.db_path))
    eng.running = True
    return s, feed, gw, eng


# ---- A: failing cancel must not freeze the engine ----------------------------
class CancelFailsGateway(StubGateway):
    async def cancel(self, order_id):
        raise RuntimeError("order not found / market closed")


async def test_cancel_error_does_not_freeze_on_window_change(tmp_path):
    _, feed, gw, eng = make(tmp_path, gw=CancelFailsGateway())
    feed.price = 100.3
    await eng.tick(T0 + 100)
    assert eng.window.position and eng.window.position.resting
    await eng.tick(T0 + 301)  # window change → cancel raises
    assert eng.window is not None and eng.window.market.start == T0 + 300
    await eng.tick(T0 + 302)
    assert eng.window.market.start == T0 + 300
    assert any("cancel failed" in e["msg"] and e["level"] == "error" for e in eng.events)


async def test_pause_cancel_does_not_raise_on_gateway_error(tmp_path):
    _, feed, gw, eng = make(tmp_path, gw=CancelFailsGateway())
    feed.price = 100.3
    await eng.tick(T0 + 100)
    await eng._cancel_resting("paused by user")  # must not raise
    assert eng.window.position is not None  # state unknown → still tracked


async def test_settlement_price_error_does_not_freeze(tmp_path):
    class BadCloseFeed(StubFeed):
        broken = False

        async def open_at(self, ts):
            if self.broken:
                raise RuntimeError("price history unavailable")
            return await super().open_at(ts)

        async def spot(self):
            return self.price

    feed = BadCloseFeed()
    _, _, gw, eng = make(tmp_path, feed=feed)
    feed.price = 100.3
    await eng.tick(T0 + 100)
    gw.books["up"] = Book(0.55, 0.56)
    await eng.tick(T0 + 101)
    assert eng.window.position.filled > 0
    feed.broken = True
    with pytest.raises(RuntimeError):  # opening the next window also needs open_at; that error is the tick's
        await eng.tick(T0 + 301)
    assert eng.window is None  # but the old window is gone, not stuck
    feed.broken = False
    await eng.tick(T0 + 312)
    assert eng.window is not None and eng.window.market.start == T0 + 300


# ---- B: degenerate volatility ------------------------------------------------
class NoHistoryFeed(StubFeed):
    calls = 0

    async def minute_closes(self, n):
        self.calls += 1
        return []


class RaisingHistoryFeed(StubFeed):
    calls = 0

    async def minute_closes(self, n):
        self.calls += 1
        raise ConnectionError("klines endpoint down")


async def test_no_volatility_means_no_trade(tmp_path):
    feed = NoHistoryFeed()
    _, _, gw, eng = make(tmp_path, feed=feed)
    feed.price = 100.3
    await eng.tick(T0 + 100)
    assert eng.window.position is None
    assert "volatility" in eng.snapshot.signal["reason"]
    assert eng.store.recent(5) == []


async def test_minute_closes_error_does_not_kill_tick(tmp_path):
    feed = RaisingHistoryFeed()
    _, _, gw, eng = make(tmp_path, feed=feed)
    feed.price = 100.3
    for i in range(30):
        await eng.tick(T0 + 100 + i)
    assert eng.snapshot.ts == T0 + 129  # every tick completed
    assert eng.window.position is None
    assert 1 <= feed.calls <= 4  # retried about every 10s, not every tick
    warns = [e for e in eng.events if "volatility refresh failed" in e["msg"]]
    assert len(warns) == feed.calls


async def test_minute_closes_error_keeps_previous_sigma(tmp_path):
    _, feed, gw, eng = make(tmp_path)
    await eng.tick(T0 + 10)
    sigma = eng._sigma
    assert sigma > 0

    async def boom(n):
        raise ConnectionError("down")

    feed.minute_closes = boom
    await eng.tick(T0 + 80)
    assert eng._sigma == sigma


# ---- C: fill racing the cancel -----------------------------------------------
class FillsAtCancelGateway(StubGateway):
    """The order fills on the exchange right as we cancel it (polls before showed nothing)."""

    async def filled(self, order_id):
        return self._orders[order_id].filled

    async def cancel(self, order_id):
        o = self._orders[order_id]
        o.filled = o.size  # matched just before the cancel landed
        await super().cancel(order_id)


async def test_fill_at_cancel_keeps_position_and_blocks_reentry(tmp_path):
    _, feed, gw, eng = make(tmp_path, gw=FillsAtCancelGateway())
    feed.price = 100.3
    await eng.tick(T0 + 100)
    pos = eng.window.position
    feed.price = 100.0  # edge gone → cancel
    await eng.tick(T0 + 101)
    assert eng.window.position is pos
    assert pos.filled == pos.size and not pos.resting
    feed.price = 100.3  # edge back: must not open a second order
    await eng.tick(T0 + 102)
    rows = eng.store.recent(10)
    assert len(rows) == 1
    assert rows[0]["status"] == "filled" and rows[0]["shares"] == pos.size
    assert rows[0]["cost"] == pytest.approx(pos.size * pos.price)


# ---- D: redeem must not block the loop ----------------------------------------
class SlowRedeemGateway(StubGateway):
    simulated = False
    name = "live-stub"

    def __init__(self):
        super().__init__()
        self.release = asyncio.Event()
        self.redeems = 0

    async def redeem_all(self):
        self.redeems += 1
        await self.release.wait()
        return 1


async def test_slow_redeem_runs_in_background(tmp_path):
    gw = SlowRedeemGateway()
    _, feed, _, eng = make(tmp_path, gw=gw)
    await eng.tick(T0 + 100)
    await asyncio.wait_for(eng.tick(T0 + 301), timeout=1)  # would hang if redeem were awaited inline
    assert eng.window is not None and eng.window.market.start == T0 + 300
    await asyncio.wait_for(eng.tick(T0 + 601), timeout=1)
    await asyncio.sleep(0)
    assert gw.redeems == 1  # no second redeem while the first is in flight
    gw.release.set()
    await asyncio.sleep(0.01)
    assert any("redeemed 1" in e["msg"] for e in eng.events)
    await eng.stop_loop()


async def test_stop_loop_cancels_pending_redeem(tmp_path):
    gw = SlowRedeemGateway()
    _, feed, _, eng = make(tmp_path, gw=gw)
    await eng.tick(T0 + 100)
    await asyncio.wait_for(eng.tick(T0 + 301), timeout=1)
    task = eng._redeem_task
    assert task is not None and not task.done()
    await asyncio.wait_for(eng.stop_loop(), timeout=1)
    assert task.done()


# ---- E: balance polling -------------------------------------------------------
class CountingGateway(StubGateway):
    balance_calls = 0

    async def balance(self):
        self.balance_calls += 1
        return await super().balance()


async def test_balance_is_cached_across_ticks(tmp_path):
    gw = CountingGateway()
    _, feed, _, eng = make(tmp_path, gw=gw)
    feed.price = 100.0  # no edge → hold every tick
    for i in range(100):
        await eng.tick(T0 + 100 + i * 0.5)
    assert gw.balance_calls <= 3


async def test_balance_refreshed_after_order_and_cancel(tmp_path):
    gw = CountingGateway()
    _, feed, _, eng = make(tmp_path, gw=gw)
    feed.price = 100.3
    await eng.tick(T0 + 100)  # posts an order, reserving funds
    await eng.tick(T0 + 101)
    assert eng.snapshot.balance < 100
    await eng.tick(T0 + 250)  # cancel inside the stop band → reserve released
    await eng.tick(T0 + 251)
    assert eng.snapshot.balance == pytest.approx(100)
