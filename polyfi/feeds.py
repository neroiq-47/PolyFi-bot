"""BTC spot price feeds.

Polymarket resolves the up/down markets on Chainlink's BTC/USD stream; exchange
spot is a close proxy, which is what we use for the fair-value estimate.
"""

from __future__ import annotations

import math
import random
import time
from datetime import datetime, timezone

import httpx


class PriceFeed:
    name = "base"

    async def spot(self) -> float:
        raise NotImplementedError

    async def minute_closes(self, n: int) -> list[float]:
        raise NotImplementedError

    async def open_at(self, ts: int) -> float | None:
        """Price at unix second ``ts`` (open of that 1-minute candle)."""
        raise NotImplementedError

    async def close(self) -> None:
        pass


class DemoFeed(PriceFeed):
    """Random-walk BTC for offline demos (about 0.05% per minute volatility)."""

    name = "demo"

    def __init__(self, start: float = 108_000.0, sigma_per_s: float = 0.0005 / math.sqrt(60), seed: int | None = None):
        self._rng = random.Random(seed)
        self.sigma = sigma_per_s
        self._history: list[tuple[float, float]] = []
        self._price = start
        self._last = time.time()
        # Seed an hour of minute history so volatility is available immediately.
        t = self._last - 3600
        p = start
        while t < self._last:
            p *= math.exp(self._rng.gauss(0, self.sigma * math.sqrt(60)))
            self._history.append((t, p))
            t += 60
        self._price = p
        self._history.append((self._last, p))

    def _advance(self) -> None:
        now = time.time()
        dt = max(0.0, now - self._last)
        if dt:
            self._price *= math.exp(self._rng.gauss(0, self.sigma * math.sqrt(dt)))
            self._last = now
            self._history.append((now, self._price))
            cutoff = now - 4000
            if self._history[0][0] < cutoff:
                self._history = [h for h in self._history if h[0] >= cutoff]

    async def spot(self) -> float:
        self._advance()
        return self._price

    async def minute_closes(self, n: int) -> list[float]:
        self._advance()
        out, next_t = [], self._last - 60 * n
        for t, p in self._history:
            if t >= next_t:
                out.append(p)
                next_t = t + 60
        return out[-n:]

    async def open_at(self, ts: int) -> float | None:
        self._advance()
        best = None
        for t, p in self._history:
            if t <= ts:
                best = p
            else:
                return best if best is not None else p
        return best


class CoinbaseFeed(PriceFeed):
    """Coinbase Exchange public REST (no key; reachable from the US)."""

    name = "coinbase"
    BASE = "https://api.exchange.coinbase.com/products/BTC-USD"

    def __init__(self) -> None:
        self._http = httpx.AsyncClient(timeout=5.0, headers={"User-Agent": "polyfi-bot"})

    async def spot(self) -> float:
        r = await self._http.get(f"{self.BASE}/ticker")
        r.raise_for_status()
        return float(r.json()["price"])

    async def _candles(self, start: int, end: int) -> list[list[float]]:
        iso = lambda s: datetime.fromtimestamp(s, timezone.utc).isoformat()  # noqa: E731
        r = await self._http.get(
            f"{self.BASE}/candles", params={"granularity": 60, "start": iso(start), "end": iso(end)}
        )
        r.raise_for_status()
        return sorted(r.json(), key=lambda c: c[0])  # [time, low, high, open, close, volume]

    async def minute_closes(self, n: int) -> list[float]:
        now = int(time.time())
        return [c[4] for c in await self._candles(now - 60 * (n + 1), now)][-n:]

    async def open_at(self, ts: int) -> float | None:
        minute = ts - ts % 60
        candles = await self._candles(minute, minute + 60)
        for c in candles:
            if int(c[0]) == minute:
                return float(c[3])
        return None

    async def close(self) -> None:
        await self._http.aclose()


class KrakenFeed(PriceFeed):
    """Kraken public REST — a second, independent BTC/USD source."""

    name = "kraken"
    BASE = "https://api.kraken.com/0/public"

    def __init__(self, http: httpx.AsyncClient | None = None) -> None:
        self._http = http or httpx.AsyncClient(timeout=5.0, headers={"User-Agent": "polyfi-bot"})

    async def _get(self, path: str, **params) -> dict:
        r = await self._http.get(f"{self.BASE}/{path}", params={"pair": "XBTUSD", **params})
        r.raise_for_status()
        body = r.json()
        if body.get("error"):
            raise RuntimeError(f"kraken: {body['error']}")
        return next(v for k, v in body["result"].items() if k != "last")

    async def spot(self) -> float:
        return float((await self._get("Ticker"))["c"][0])

    async def _ohlc(self, since: int) -> list[list]:
        return sorted(await self._get("OHLC", interval=1, since=since), key=lambda c: c[0])

    async def minute_closes(self, n: int) -> list[float]:
        rows = await self._ohlc(int(time.time()) - 60 * (n + 1))
        return [float(c[4]) for c in rows][-n:]  # [time, open, high, low, close, vwap, volume, count]

    async def open_at(self, ts: int) -> float | None:
        minute = ts - ts % 60
        for c in await self._ohlc(minute - 60):
            if int(c[0]) == minute:
                return float(c[1])
        return None

    async def close(self) -> None:
        await self._http.aclose()


class FallbackFeed(PriceFeed):
    """Try each feed in order; the first that answers wins. ``last_source`` says which one."""

    def __init__(self, *feeds: PriceFeed) -> None:
        self.feeds = feeds
        self.name = feeds[0].name
        self.last_source = feeds[0].name

    async def _first(self, method: str, *args, accept=lambda v: True):
        errors = []
        for feed in self.feeds:
            try:
                value = await getattr(feed, method)(*args)
            except Exception as err:
                errors.append(f"{feed.name}: {err}")
                continue
            if accept(value):
                self.last_source = feed.name
                return value
            errors.append(f"{feed.name}: no data")
        raise RuntimeError("all BTC price sources failed — " + "; ".join(errors))

    async def spot(self) -> float:
        return await self._first("spot")

    async def minute_closes(self, n: int) -> list[float]:
        return await self._first("minute_closes", n, accept=lambda v: len(v) >= 3)

    async def open_at(self, ts: int) -> float | None:
        try:
            return await self._first("open_at", ts, accept=lambda v: v is not None)
        except RuntimeError:
            return None

    async def close(self) -> None:
        for feed in self.feeds:
            await feed.close()
