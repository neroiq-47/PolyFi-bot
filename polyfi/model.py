"""Pricing math: window slugs, fair probability of "Up", and Polymarket taker fees."""

from __future__ import annotations

import math
from decimal import Decimal
from dataclasses import dataclass

# Crypto category taker fee rate from docs.polymarket.com/trading/fees (Oct 2026).
CRYPTO_TAKER_FEE_RATE = 0.07


def window_start(now: float, window_seconds: int) -> int:
    return int(now // window_seconds) * window_seconds


def window_slug(asset: str, start: int, window_seconds: int) -> str:
    """Polymarket up/down slug, e.g. ``btc-updown-5m-1773879000`` (start = window open, unix s)."""
    minutes = window_seconds // 60
    label = f"{minutes}m" if minutes < 60 else f"{minutes // 60}h"
    return f"{asset}-updown-{label}-{start}"


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def prob_up(price: float, open_price: float, sigma_per_s: float, seconds_left: float) -> float:
    """P(close >= open) under a driftless random walk in log price.

    Up wins ties, so at the very end an exactly-equal price counts as Up.
    """
    if not all(math.isfinite(x) for x in (price, open_price, seconds_left)):
        return 0.5
    if price <= 0 or open_price <= 0:
        return 0.5
    if seconds_left <= 0:
        return 1.0 if price >= open_price else 0.0
    # No (or broken) volatility estimate: we know nothing about the outcome yet.
    # Returning 1/0 here would fake a huge edge, so stay neutral.
    if not math.isfinite(sigma_per_s) or sigma_per_s <= 0:
        return 0.5
    z = math.log(price / open_price) / (sigma_per_s * math.sqrt(seconds_left))
    return min(0.995, max(0.005, norm_cdf(z)))


def sigma_per_second(closes: list[float]) -> float:
    """Per-second volatility from a list of 1-minute closes."""
    if len(closes) < 3:
        return 0.0
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    if len(rets) < 2:
        return 0.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) / math.sqrt(60.0)


def taker_fee(shares: float, price: float, fee_rate: float = CRYPTO_TAKER_FEE_RATE) -> float:
    """fee = C × feeRate × p × (1 − p), rounded to 5 dp (makers pay 0)."""
    return round(shares * fee_rate * price * (1.0 - price), 5)


def round_down_to_tick(price: float, tick: float) -> float:
    return round(math.floor(round(price / tick, 6)) * tick, 6)


def _tick_decimals(tick: float) -> int:
    exp = Decimal(repr(tick)).normalize().as_tuple().exponent
    return max(0, -int(exp))


def _ticks(price: float, tick: float) -> int:
    """Book price -> whole number of ticks (book prices sit on the grid)."""
    return int(round(price / tick))


def _from_ticks(n: int, tick: float) -> float:
    """Whole ticks -> clean float on the tick grid (e.g. 0.56, not 0.5599999999999999)."""
    return round(n * tick, _tick_decimals(tick))


@dataclass
class Decision:
    side: str | None          # "UP", "DOWN" or None
    p_up: float
    price: float = 0.0        # limit price we are willing to pay
    edge: float = 0.0
    reason: str = ""


def decide(
    p_up: float,
    up_bid: float | None,
    up_ask: float | None,
    down_bid: float | None,
    down_ask: float | None,
    *,
    min_edge: float,
    free_only: bool,
    tick: float = 0.01,
) -> Decision:
    """Pick the side with the larger edge.

    free_only: join the book one tick above the best bid but never cross the ask
    (post-only maker order → no fee); on a 1-tick spread, join the bid itself.
    Otherwise take the ask and pay the fee. Returned prices sit exactly on the tick grid.
    """
    best: Decision = Decision(None, p_up, reason="no edge")
    # All price arithmetic happens in integer ticks so results land exactly on the grid.
    for side, fair, bid, ask in (("UP", p_up, up_bid, up_ask), ("DOWN", 1 - p_up, down_bid, down_ask)):
        if free_only:
            if bid is None:
                continue
            b = _ticks(bid, tick)
            n = b + 1
            if ask is not None and n >= _ticks(ask, tick):
                # 1-tick spread: can't improve without crossing, so join the bid.
                n = _ticks(ask, tick) - 1
            cap = math.floor(round((fair - min_edge) / tick, 6))
            n = min(n, cap)
            if n < 1 or n < b:
                continue
            price = _from_ticks(n, tick)
            cost = price
        else:
            if ask is None:
                continue
            price = _from_ticks(_ticks(ask, tick), tick)
            cost = price + taker_fee(1, price)
        edge = fair - cost
        if edge >= min_edge - 1e-9 and edge > best.edge:
            best = Decision(side, p_up, price, round(edge, 4), "maker" if free_only else "taker")
    return best
