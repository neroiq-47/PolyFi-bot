"""Pricing math: window slugs, fair probability of "Up", and Polymarket taker fees."""

from __future__ import annotations

import math
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
    if price <= 0 or open_price <= 0:
        return 0.5
    if seconds_left <= 0 or sigma_per_s <= 0:
        return 1.0 if price >= open_price else 0.0
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
    (post-only maker order → no fee). Otherwise take the ask and pay the fee.
    """
    best: Decision = Decision(None, p_up, reason="no edge")
    for side, fair, bid, ask in (("UP", p_up, up_bid, up_ask), ("DOWN", 1 - p_up, down_bid, down_ask)):
        if free_only:
            if bid is None:
                continue
            price = round(bid + tick, 6)
            if ask is not None and price >= ask:
                price = ask - tick
            price = min(price, round_down_to_tick(fair - min_edge, tick))
            if price < tick or (bid is not None and price < bid):
                continue
            cost = price
        else:
            if ask is None:
                continue
            price = ask
            cost = ask + taker_fee(1, ask)
        edge = fair - cost
        if edge >= min_edge - 1e-9 and edge > best.edge:
            best = Decision(side, p_up, round(price, 4), round(edge, 4), "maker" if free_only else "taker")
    return best
