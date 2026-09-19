"""Fee schedules and the edge arithmetic every detector relies on.

All fees are taker fees: an arbitrage bot crosses the spread, it never rests
orders. Defaults are the lowest public retail tier of each venue as of 2026;
override them in config to match your own account. They are deliberately
conservative — a scanner that flatters its own fees "finds" free money.
"""
from __future__ import annotations

# When the defaults below were last checked against the venues' published schedules.
FEES_VERIFIED_ON = "2026-09-19"

# taker fee in basis points (1 bps = 0.01%)
DEFAULT_TAKER_BPS: dict[str, float] = {
    "binance": 10.0,  # 0.10% VIP 0 (0.075% when paying fees in BNB; 0.095% on USDC pairs)
    "coinbase": 60.0,  # 0.60% Coinbase Exchange $0-10K tier; fresh retail Advanced accounts pay 0.90-1.20%
    "kraken": 80.0,  # 0.80% Kraken Pro tier 1 as published 2026-09; older references still say 0.40%
}


class FeeSchedule:
    """Per-venue taker fee lookup, as fractions (10 bps -> 0.001)."""

    def __init__(self, taker_bps: dict[str, float] | None = None):
        merged = dict(DEFAULT_TAKER_BPS)
        if taker_bps:
            merged.update({k.lower(): float(v) for k, v in taker_bps.items()})
        self._taker = {k: v / 1e4 for k, v in merged.items()}

    def taker(self, venue: str) -> float:
        try:
            return self._taker[venue]
        except KeyError:
            raise KeyError(f"no taker fee configured for venue {venue!r}") from None

    def as_bps(self) -> dict[str, float]:
        return {k: v * 1e4 for k, v in self._taker.items()}


def cross_edge(buy_ask: float, buy_fee: float, sell_bid: float, sell_fee: float) -> tuple[float, float, float]:
    """Edge of buying one unit at `buy_ask` and selling it at `sell_bid`.

    Fees are fractions and are charged on the notional in the quote currency
    (the convention the paper executor uses too): buying costs
    `ask * (1 + buy_fee)`, selling yields `bid * (1 - sell_fee)`.
    Returns (gross_bps, net_bps, net_profit_per_unit), both bps figures
    relative to the cash outlay `buy_ask`. Both prices must share a quote
    currency.
    """
    if buy_ask <= 0 or sell_bid <= 0:
        return float("-inf"), float("-inf"), float("-inf")
    gross_bps = (sell_bid / buy_ask - 1.0) * 1e4
    net_per_unit = sell_bid * (1.0 - sell_fee) - buy_ask * (1.0 + buy_fee)
    net_bps = net_per_unit / buy_ask * 1e4
    return gross_bps, net_bps, net_per_unit


def leg_net_rate(rate: float, fee: float, side: str) -> float:
    """Conversion rate of one leg after its taker fee.

    `rate` is units of the next asset per unit of the current asset at the top
    of book. A sell leg keeps `rate * (1 - fee)`; a buy leg pays `(1 + fee)`
    times the notional, so it keeps `rate / (1 + fee)`.
    """
    return rate * (1.0 - fee) if side == "sell" else rate / (1.0 + fee)


def cycle_rate(rates: list[float], fees: list[float], sides: list[str]) -> tuple[float, float]:
    """Multiply conversion rates around a cycle.

    Returns (gross_multiplier, net_multiplier): 1.0 means break-even.
    """
    gross = 1.0
    net = 1.0
    for r, f, s in zip(rates, fees, sides):
        gross *= r
        net *= leg_net_rate(r, f, s)
    return gross, net


def bps(multiplier: float) -> float:
    return (multiplier - 1.0) * 1e4


def cross_break_even_bps(buy_fee: float, sell_fee: float, haircut_bps: float = 0.0) -> float:
    """Gross spread (bps of the buy price) at which a cross-venue trade nets zero:
    bid/ask = (1+f_buy)/(1-f_sell), plus the stablecoin haircut."""
    return ((1.0 + buy_fee) / (1.0 - sell_fee) - 1.0) * 1e4 + haircut_bps


def triangle_break_even_bps(fee: float, legs: int = 3) -> float:
    """Gross cycle multiplier (bps) at which a same-venue cycle nets zero; the exact
    figure depends on the buy/sell mix, this is the all-sell (1-f)^n form."""
    return (1.0 / (1.0 - fee) ** legs - 1.0) * 1e4
