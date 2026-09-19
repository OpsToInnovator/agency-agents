import math
from decimal import Decimal

import pytest

from arbbot.fees import DEFAULT_TAKER_BPS, FeeSchedule, bps, cross_edge, cycle_rate, leg_net_rate
from arbbot.filters import parse_binance_filters, round_step, round_tick, size_order
from tests.helpers import BTC_BINANCE


def test_cross_edge_break_even_needs_both_fees():
    # 10 bps + 60 bps of fees: a 70 bps gross spread is (almost exactly) break-even
    gross, net, per_unit = cross_edge(100.0, 0.001, 100.70, 0.006)
    assert gross == pytest.approx(70.0)
    assert net == pytest.approx(-0.42, abs=0.01)  # 100.70*0.994 - 100.1 = -0.0042 -> -0.42 bps
    gross, net, _ = cross_edge(100.0, 0.001, 100.75, 0.006)
    assert net > 0


def test_cross_edge_rejects_bad_prices():
    assert cross_edge(0, 0.001, 1, 0.001)[1] == float("-inf")


def test_leg_net_rate_sides():
    assert leg_net_rate(2.0, 0.001, "sell") == pytest.approx(2.0 * 0.999)
    assert leg_net_rate(2.0, 0.001, "buy") == pytest.approx(2.0 / 1.001)


def test_cycle_rate_three_fees():
    gross, net = cycle_rate([1 / 100.0, 50.0, 2.0], [0.001] * 3, ["buy", "sell", "sell"])
    assert gross == pytest.approx(1.0)
    assert bps(net) == pytest.approx(-30.0, abs=0.05)


def test_default_fees_are_conservative():
    assert DEFAULT_TAKER_BPS["binance"] >= 10
    assert DEFAULT_TAKER_BPS["coinbase"] >= 40
    assert DEFAULT_TAKER_BPS["kraken"] >= 40
    fs = FeeSchedule({"binance": 7.5})
    assert fs.taker("binance") == pytest.approx(0.00075)
    assert fs.taker("kraken") == pytest.approx(DEFAULT_TAKER_BPS["kraken"] / 1e4)
    with pytest.raises(KeyError):
        fs.taker("ftx")


def test_round_step_never_rounds_up():
    assert round_step(0.123456789, 0.00001) == Decimal("0.12345")
    assert round_step(1.0, None) == Decimal("1.0")
    assert round_step(0.5, 0) == Decimal("0.5")


def test_round_tick_direction():
    assert round_tick(100.004, 0.01, "buy") == Decimal("100.01")
    assert round_tick(100.004, 0.01, "sell") == Decimal("100.00")


def test_size_order_min_notional_and_qty():
    p, q, reason = size_order(BTC_BINANCE, 80000.0, 0.00001, "buy")
    assert reason and "min_notional" in reason
    p, q, reason = size_order(BTC_BINANCE, 80000.0, 0.000001, "buy")
    assert reason == "quantity rounds to zero"
    p, q, reason = size_order(BTC_BINANCE, 80000.005, 0.001234567, "buy")
    assert reason is None
    assert q == Decimal("0.00123")
    assert p == Decimal("80000.01")


def test_parse_binance_filters_handles_zero_steps():
    info = {"filters": [
        {"filterType": "PRICE_FILTER", "tickSize": "0.01000000"},
        {"filterType": "LOT_SIZE", "stepSize": "0.00001000", "minQty": "0.00001000"},
        {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.00000000", "minQty": "0.00000000"},
        {"filterType": "NOTIONAL", "minNotional": "5.00000000"},
    ]}
    f = parse_binance_filters(info)
    assert f == {"tick_size": 0.01, "step_size": 0.00001, "min_qty": 0.00001, "min_notional": 5.0}
    assert math.isclose(f["step_size"], 1e-5)
