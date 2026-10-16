"""WebSocket streams: a live top-of-book cache and a Chainlink BTC/USD price feed.

Both run as background tasks and fall back to REST (books) or Coinbase (price)
whenever the stream is stale, so a dropped socket never stops the engine.
"""

from __future__ import annotations

import asyncio
import bisect
import logging
import time
from typing import Any

from .feeds import PriceFeed
from .gateways import Book

log = logging.getLogger("polyfi.streams")


class BookCache:
    """Best bid/ask per asset, kept current from the CLOB market channel."""

    def __init__(self) -> None:
        self._books: dict[str, tuple[float | None, float | None, float]] = {}
        self._assets: tuple[str, ...] = ()
        self._task: asyncio.Task | None = None

    def get(self, asset: str, max_age: float = 5.0) -> Book | None:
        entry = self._books.get(asset)
        if entry is None or time.time() - entry[2] > max_age:
            return None
        return Book(entry[0], entry[1])

    def _set(self, asset: str, bid, ask) -> None:
        self._books[str(asset)] = (
            float(bid) if bid is not None else None,
            float(ask) if ask is not None else None,
            time.time(),
        )

    def apply(self, event: Any) -> None:
        kind, p = getattr(event, "type", None), getattr(event, "payload", None)
        if p is None:
            return
        if kind == "book":
            bid = max((lv.price for lv in p.bids), default=None)
            ask = min((lv.price for lv in p.asks), default=None)
            self._set(p.asset_id, bid, ask)
        elif kind == "best_bid_ask":
            self._set(p.asset_id, p.best_bid, p.best_ask)
        elif kind == "price_change":
            # A price_change may carry only one side; keep the cached value for the other.
            for ch in p.price_changes:
                bid, ask = ch.best_bid, ch.best_ask
                if bid is None and ask is None:
                    continue
                prev = self._books.get(str(ch.asset_id))
                if prev is not None:
                    bid = prev[0] if bid is None else bid
                    ask = prev[1] if ask is None else ask
                self._set(ch.asset_id, bid, ask)

    def watch(self, client, assets: tuple[str, ...]) -> None:
        """(Re)subscribe when the window's assets change."""
        if assets == self._assets and self._task and not self._task.done():
            return
        self._assets = assets
        if self._task:
            self._task.cancel()
        self._books = {a: v for a, v in self._books.items() if a in assets}
        self._task = asyncio.create_task(self._run(client, assets))

    async def _run(self, client, assets: tuple[str, ...]) -> None:
        from polymarket.streams import MarketSpec

        delay = 1.0
        while True:
            try:
                async with await client.subscribe(MarketSpec(asset_ids=list(assets), custom_feature_enabled=True)) as stream:
                    delay = 1.0
                    async for event in stream:
                        self.apply(event)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                log.warning("book stream dropped (%s); retrying in %.0fs", err, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)

    async def close(self) -> None:
        if self._task:
            self._task.cancel()


class ChainlinkFeed(PriceFeed):
    """Chainlink BTC/USD via Polymarket's authenticated price stream — the resolution source.

    Falls back to ``fallback`` (Coinbase) when the stream is stale or lacks history.
    """

    name = "chainlink"

    def __init__(self, client, fallback: PriceFeed, symbol: str = "btcusd", stale_after: float = 10.0) -> None:
        self._client, self.fallback, self.symbol, self.stale_after = client, fallback, symbol, stale_after
        self._ts: list[float] = []
        self._px: list[float] = []
        self._task: asyncio.Task | None = None
        self.last_source = "fallback"

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    def add(self, ts: float, price: float) -> None:
        if self._ts and ts < self._ts[-1]:
            i = bisect.bisect(self._ts, ts)
            self._ts.insert(i, ts)
            self._px.insert(i, price)
        else:
            self._ts.append(ts)
            self._px.append(price)
        cutoff = time.time() - 2 * 3600
        if self._ts[0] < cutoff:
            k = bisect.bisect(self._ts, cutoff)
            del self._ts[:k], self._px[:k]

    def apply(self, event: Any) -> None:
        p = getattr(event, "payload", None)
        if p is None:
            return
        if getattr(event, "type", None) == "subscribe":
            for pt in p.data:
                self.add(pt.timestamp.timestamp(), float(pt.value))
        else:
            self.add(p.timestamp.timestamp(), float(p.value))

    async def _run(self) -> None:
        from polymarket.streams import CryptoPriceSpec

        delay = 1.0
        while True:
            try:
                async with await self._client.subscribe(CryptoPriceSpec(symbols=[self.symbol], provider="chainlink")) as stream:
                    delay = 1.0
                    async for event in stream:
                        self.apply(event)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                log.warning("chainlink stream dropped (%s); retrying in %.0fs", err, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)

    def _fresh(self) -> bool:
        return bool(self._ts) and time.time() - self._ts[-1] <= self.stale_after

    async def spot(self) -> float:
        if self._fresh():
            self.last_source = "chainlink"
            return self._px[-1]
        self.last_source = "fallback"
        return await self.fallback.spot()

    async def open_at(self, ts: int) -> float | None:
        # Price in effect at ts: the last tick at or before it (must be within 5s).
        i = bisect.bisect_right(self._ts, ts) - 1
        if i >= 0 and ts - self._ts[i] <= 5:
            return self._px[i]
        return await self.fallback.open_at(ts)

    async def minute_closes(self, n: int) -> list[float]:
        now = time.time()
        if self._ts and self._ts[0] <= now - 60 * n:
            out = []
            for k in range(n, 0, -1):
                i = bisect.bisect_right(self._ts, now - 60 * (k - 1)) - 1
                if i >= 0:
                    out.append(self._px[i])
            if len(out) >= n - 1:
                return out
        return await self.fallback.minute_closes(n)

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
        await self.fallback.close()
