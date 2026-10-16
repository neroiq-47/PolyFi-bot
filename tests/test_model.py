import pytest

from polyfi.model import decide, prob_up, sigma_per_second, taker_fee, window_slug, window_start


def test_window_slug_matches_polymarket_format():
    start = window_start(1773879123.4, 300)
    assert start == 1773879000
    assert window_slug("btc", start, 300) == "btc-updown-5m-1773879000"
    assert window_slug("btc", 1773879300, 900) == "btc-updown-15m-1773879300"


def test_prob_up_is_symmetric_and_resolves_at_expiry():
    assert prob_up(100, 100, 0.0001, 120) == pytest.approx(0.5)
    assert prob_up(101, 100, 0.0001, 120) > 0.9
    assert prob_up(99, 100, 0.0001, 120) < 0.1
    assert prob_up(100, 100, 0.0001, 0) == 1.0  # ties resolve Up
    assert prob_up(99.99, 100, 0.0001, 0) == 0.0


def test_sigma_per_second_scales_minute_vol():
    closes = [100 * (1.001 if i % 2 else 1.0) for i in range(30)]
    s = sigma_per_second(closes)
    assert 0 < s < 0.001


def test_taker_fee_matches_docs_table():
    # docs.polymarket.com/trading/fees, crypto: 100 shares at $0.50 → $1.75, at $0.10 → $0.63
    assert taker_fee(100, 0.50) == pytest.approx(1.75)
    assert taker_fee(100, 0.10) == pytest.approx(0.63)


def test_free_only_never_crosses_the_spread():
    d = decide(0.70, 0.55, 0.57, 0.43, 0.45, min_edge=0.04, free_only=True)
    assert d.side == "UP"
    assert d.price == pytest.approx(0.56)   # one tick above bid, below ask
    assert d.edge >= 0.04


def test_free_only_caps_price_at_fair_minus_edge():
    d = decide(0.60, 0.55, 0.60, 0.38, 0.45, min_edge=0.04, free_only=True)
    assert d.side == "UP" and d.price <= 0.56


def test_no_trade_without_edge():
    d = decide(0.50, 0.49, 0.51, 0.49, 0.51, min_edge=0.04, free_only=True)
    assert d.side is None


def test_taker_mode_accounts_for_fee():
    # fair 0.60, ask 0.55: fee ≈ 0.0173/share → edge 0.0327 < 0.04 → no trade
    assert decide(0.60, 0.53, 0.55, 0.40, 0.47, min_edge=0.04, free_only=False).side is None
    d = decide(0.70, 0.53, 0.55, 0.30, 0.47, min_edge=0.04, free_only=False)
    assert d.side == "UP" and d.price == 0.55


def test_down_side_selected_when_price_below_open():
    d = decide(0.25, 0.20, 0.22, 0.60, 0.62, min_edge=0.04, free_only=True)
    assert d.side == "DOWN"
