"""Regression tests for audited bugs in polyfi/model.py and polyfi/config.py."""

import math

import pytest

from polyfi.config import Settings
from polyfi.model import decide, prob_up


def _on_grid(price: float, tick: float) -> bool:
    return abs(round(price / tick) * tick - price) < 1e-12


def _clean(price: float) -> bool:
    # no float tail like 0.5599999999999999
    return len(repr(price).split(".")[-1]) <= 4


# --- Bug 1: maker quote on a 1-tick spread / prices on tick grid -------------

def test_maker_quotes_on_one_tick_spread_by_joining_bid():
    d = decide(0.80, 0.56, 0.57, 0.43, 0.44, min_edge=0.04, free_only=True)
    assert d.side == "UP"
    assert d.price == 0.56
    assert repr(d.price) == "0.56"


@pytest.mark.parametrize("bid,ask", [(0.56, 0.57), (0.29, 0.30), (0.07, 0.08), (0.55, 0.57), (0.1, 0.4)])
def test_maker_price_is_on_tick_grid(bid, ask):
    d = decide(0.95, bid, ask, None, None, min_edge=0.04, free_only=True)
    assert d.side == "UP"
    assert _on_grid(d.price, 0.01) and _clean(d.price)
    assert bid <= d.price < ask


def test_maker_price_capped_by_fair_is_on_grid():
    # fair - edge = 0.33 - 0.04 = 0.29 (float: 0.29000000000000004)
    d = decide(0.33, 0.29, 0.40, None, None, min_edge=0.04, free_only=True)
    assert d.side == "UP" and d.price == 0.29 and _clean(d.price)


def test_maker_price_on_fine_tick_grid():
    d = decide(0.80, 0.561, 0.562, None, None, min_edge=0.04, free_only=True, tick=0.001)
    assert d.side == "UP"
    assert d.price == 0.561 and _on_grid(d.price, 0.001) and _clean(d.price)


def test_taker_price_is_on_tick_grid():
    d = decide(0.90, 0.56, 0.57, None, None, min_edge=0.04, free_only=False)
    assert d.side == "UP" and d.price == 0.57 and _on_grid(d.price, 0.01) and _clean(d.price)


# --- Bug 2: degenerate volatility -------------------------------------------

def test_zero_sigma_before_expiry_is_no_information():
    assert prob_up(100.01, 100, 0.0, 120) == 0.5
    assert prob_up(99.99, 100, 0.0, 120) == 0.5
    assert prob_up(100.01, 100, -1.0, 120) == 0.5


def test_non_finite_inputs_are_no_information():
    assert prob_up(100.01, 100, float("nan"), 120) == 0.5
    assert prob_up(100.01, 100, float("inf"), 120) == 0.5
    assert prob_up(float("nan"), 100, 0.0001, 120) == 0.5
    assert prob_up(100.01, float("nan"), 0.0001, 120) == 0.5
    assert prob_up(100.01, 100, 0.0001, float("nan")) == 0.5


def test_zero_sigma_tiny_move_does_not_trade():
    p = prob_up(100.01, 100, 0.0, 120)
    for free_only in (True, False):
        assert decide(p, 0.49, 0.51, 0.49, 0.51, min_edge=0.04, free_only=free_only).side is None


def test_expiry_behaviour_unchanged_including_ties():
    assert prob_up(100, 100, 0.0, 0) == 1.0  # tie resolves Up
    assert prob_up(100, 100, 0.0001, 0) == 1.0
    assert prob_up(100.01, 100, 0.0, 0) == 1.0
    assert prob_up(99.99, 100, 0.0, 0) == 0.0
    assert prob_up(99.99, 100, 0.0001, -5) == 0.0


# --- Bug 3: Settings.update atomicity and NaN/inf ---------------------------

def test_update_low_stake_leaves_settings_unchanged():
    s = Settings()
    with pytest.raises(ValueError):
        s.update({"stake_usd": 0.5})
    assert s.stake_usd == 5.0


def test_update_unknown_key_applies_nothing():
    s = Settings()
    with pytest.raises(ValueError):
        s.update({"stake_usd": 9, "bogus": 1})
    assert s.stake_usd == 5.0


def test_update_bad_later_value_applies_nothing():
    s = Settings()
    with pytest.raises(ValueError):
        s.update({"stake_usd": 9, "min_edge": -0.1})
    assert s.stake_usd == 5.0 and s.min_edge == 0.04


@pytest.mark.parametrize("key", ["stake_usd", "min_edge", "max_daily_loss_usd", "entry_after_s"])
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), "nan", "inf"])
def test_update_rejects_non_finite(key, bad):
    s = Settings()
    before = getattr(s, key)
    with pytest.raises(ValueError):
        s.update({key: bad})
    after = getattr(s, key)
    assert after == before and not (isinstance(after, float) and math.isnan(after))


def test_update_rejects_min_edge_of_one_or_more():
    s = Settings()
    with pytest.raises(ValueError):
        s.update({"min_edge": 1})
    assert s.min_edge == 0.04


def test_update_valid_changes_apply_together():
    s = Settings()
    s.update({"stake_usd": "9", "min_edge": 0.05, "free_only": "false", "entry_after_s": 30})
    assert s.stake_usd == 9.0 and s.min_edge == 0.05 and s.free_only is False and s.entry_after_s == 30
