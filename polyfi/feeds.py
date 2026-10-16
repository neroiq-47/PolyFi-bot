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
