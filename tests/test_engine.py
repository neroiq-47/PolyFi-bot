import pytest

from polyfi.config import Settings
from polyfi.engine import Engine
from polyfi.feeds import PriceFeed
from polyfi.gateways import Book, WindowMarket, _SimFills
from polyfi.store import Store


class StubFeed(PriceFeed):
    def __init__(self, price=100.0):
        self.price = price

    async def spot(self):
        return self.price

    async def minute_closes(self, n):
        return [100 * (1.0005 if i % 2 else 1.0) for i in range(n)]

    async def open_at(self, ts):
        return 100.0 if ts % 300 == 0 and ts < 10_000_000 else None


class StubGateway(_SimFills):
    name = "demo"

    def __init__(self):
        super().__init__(starting_balance=100.0)
        self.books = {"up": Book(0.55, 0.57), "down": Book(0.43, 0.45)}
        self.missing = False

    async def find_market(self, slug, start, end):
        if self.missing:
            return None
        return WindowMarket(slug, start, end, "up", "down")

    async def book(self, asset):
        return self.books[asset]


@pytest.fixture
def setup(tmp_path):
    s = Settings(db_path=str(tmp_path / "t.db"), stake_usd=5, min_edge=0.04, entry_after_s=20, stop_before_end_s=60)
    feed, gw = StubFeed(), StubGateway()
    eng = Engine(s, feed, gw, Store(s.db_path))
    eng.running = True
    return s, feed, gw, eng


T0 = 3000  # a window boundary (multiple of 300)


async def test_paused_bot_does_not_trade(setup):
    _, feed, gw, eng = setup
    eng.running = False
    feed.price = 100.3
    await eng.tick(T0 + 100)
    assert eng.window.position is None
    assert eng.snapshot.signal["reason"] == "bot paused"


async def test_waits_for_entry_band(setup):
    _, feed, _, eng = setup
    feed.price = 100.3
    await eng.tick(T0 + 5)
    assert eng.window.position is None
    assert "warming up" in eng.snapshot.signal["reason"]


async def test_maker_order_fill_and_win(setup):
    _, feed, gw, eng = setup
    feed.price = 100.3  # well above price to beat → Up is cheap at 0.57
    await eng.tick(T0 + 100)
    pos = eng.window.position
    assert pos and pos.side == "UP" and pos.resting and pos.price == 0.56
    gw.books["up"] = Book(0.55, 0.56)  # sellers reach our bid → filled
    await eng.tick(T0 + 101)
    assert pos.filled == pos.size and not pos.resting
    await eng.tick(T0 + 301)  # next window settles the previous one (close 100.3 ≥ 100)
    trade = eng.store.recent(1)[0]
    assert trade["status"] == "won" and trade["fee"] == 0
    assert trade["pnl"] == pytest.approx(pos.size * (1 - 0.56), abs=1e-6)
    assert await gw.balance() == pytest.approx(100 + pos.size * (1 - 0.56), abs=0.01)


async def test_unfilled_order_cancelled_and_reserve_released(setup):
    _, feed, gw, eng = setup
    feed.price = 100.3
    await eng.tick(T0 + 100)
    assert await gw.balance() < 100
    await eng.tick(T0 + 250)  # inside the stop band → cancel
    assert eng.window.position is None
    assert await gw.balance() == pytest.approx(100)
    assert eng.store.recent(1)[0]["status"] == "cancelled"


async def test_retries_after_unfilled_cancel_but_capped(setup):
    _, feed, gw, eng = setup
    for i in range(5):
        feed.price = 100.3
        await eng.tick(T0 + 100 + 2 * i)      # posts a maker bid
        feed.price = 100.0                     # edge vanishes → cancel
        await eng.tick(T0 + 101 + 2 * i)
    assert len(eng.store.recent(10)) == 3
    assert eng.snapshot.signal["reason"] == "max attempts this window"


async def test_no_second_position_once_filled(setup):
    _, feed, gw, eng = setup
    feed.price = 100.3
    await eng.tick(T0 + 100)
    gw.books["up"] = Book(0.55, 0.56)
    await eng.tick(T0 + 101)
    await eng.tick(T0 + 102)
    assert len(eng.store.recent(10)) == 1


async def test_daily_loss_limit_halts(setup):
    s, feed, gw, eng = setup
    s.max_daily_loss_usd = 1
    eng.store.add(mode="demo", slug="x", window_start=0, side="UP", kind="maker", price=0.5, shares=10, status="lost", pnl=-5)
    feed.price = 100.3
    await eng.tick(T0 + 100)
    assert eng.window.position is None
    assert "daily loss" in eng.halted_reason


async def test_missing_market_is_retried_not_spammed(setup):
    _, _, gw, eng = setup
    gw.missing = True
    await eng.tick(T0 + 1)
    await eng.tick(T0 + 2)
    assert eng.window is None
    assert sum("not listed" in e["msg"] for e in eng.events) == 1
    gw.missing = False
    await eng.tick(T0 + 12)
    assert eng.window is not None
