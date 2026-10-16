import time
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace as NS

import pytest

from polyfi.backtest import run
from polyfi.config import Settings
from polyfi.feeds import PriceFeed
from polyfi.streams import BookCache, ChainlinkFeed


def lv(p):
    return NS(price=Decimal(str(p)), size=Decimal("10"))


def test_book_cache_applies_all_event_kinds():
    c = BookCache()
    c.apply(NS(type="book", payload=NS(asset_id="a", bids=(lv(0.40), lv(0.42)), asks=(lv(0.47), lv(0.45)))))
    assert (c.get("a").bid, c.get("a").ask) == (0.42, 0.45)
    c.apply(NS(type="best_bid_ask", payload=NS(asset_id="a", best_bid=Decimal("0.43"), best_ask=Decimal("0.44"))))
    assert (c.get("a").bid, c.get("a").ask) == (0.43, 0.44)
    ch = NS(asset_id="b", best_bid=Decimal("0.55"), best_ask=Decimal("0.57"))
    c.apply(NS(type="price_change", payload=NS(price_changes=(ch,))))
    assert c.get("b").ask == 0.57
    assert c.get("missing") is None


def test_book_cache_goes_stale():
    c = BookCache()
    c.apply(NS(type="best_bid_ask", payload=NS(asset_id="a", best_bid=Decimal("0.4"), best_ask=Decimal("0.5"))))
    c._books["a"] = (*c._books["a"][:2], time.time() - 30)
    assert c.get("a") is None  # → gateway falls back to REST


class Fallback(PriceFeed):
    async def spot(self):
        return 1.0

    async def open_at(self, ts):
        return 2.0

    async def minute_closes(self, n):
        return [3.0] * n


def pt(ts, v):
    return NS(timestamp=datetime.fromtimestamp(ts, timezone.utc), value=Decimal(str(v)))


async def test_chainlink_feed_uses_stream_then_falls_back():
    now = time.time()
    f = ChainlinkFeed(client=None, fallback=Fallback())
    f.apply(NS(type="subscribe", payload=NS(data=tuple(pt(now - 3700 + 10 * i, 100 + i) for i in range(370)))))
    f.apply(NS(type="update", payload=pt(now, 500)))
    assert await f.spot() == 500 and f.last_source == "chainlink"
    assert await f.open_at(int(now - 3700 + 25)) == 102   # last tick at/before ts
    closes = await f.minute_closes(60)
    assert len(closes) >= 59 and closes[-1] == 500
    assert await f.open_at(int(now + 1000)) == 2.0           # no data near ts → fallback
    f._ts[-1] = now - 60
    assert await f.spot() == 1.0 and f.last_source == "fallback"


def synthetic_ticks(windows=4, start=3000):
    """BTC climbs in every window while the Up book lags behind fair value."""
    ticks = []
    price = 100.0
    for w in range(windows):
        ws = start + 300 * w
        open_price = price
        for s in range(300):
            price = open_price * (1 + 0.00002 * s)
            lag = 0.5 + 0.0006 * s
            ticks.append(dict(ts=ws + s, slug=f"btc-updown-5m-{ws}", window_start=ws, window_end=ws + 300,
                              btc=price, open_price=open_price, up_bid=round(lag - 0.01, 2), up_ask=round(lag + 0.01, 2),
                              down_bid=round(1 - lag - 0.01, 2), down_ask=round(1 - lag + 0.01, 2), source="test"))
    # one tick of the following window so the last window settles
    ws = start + 300 * windows
    ticks.append(dict(ticks[-1], ts=ws, window_start=ws, window_end=ws + 300, open_price=price, slug=f"btc-updown-5m-{ws}"))
    return ticks


async def test_backtest_taker_trades_and_wins_on_trending_tape():
    s = Settings(stake_usd=5, min_edge=0.04, free_only=False, entry_after_s=20, stop_before_end_s=60)
    r = await run(synthetic_ticks(), s)
    assert r["trades"] == 4 and r["win_rate"] == 1.0
    assert r["pnl"] > 0 and r["fees"] > 0


async def test_backtest_maker_never_pays_fees():
    s = Settings(stake_usd=5, min_edge=0.04, free_only=True, entry_after_s=20, stop_before_end_s=60)
    r = await run(synthetic_ticks(), s)
    assert r["fees"] == 0
    assert r["orders"] >= r["trades"]
