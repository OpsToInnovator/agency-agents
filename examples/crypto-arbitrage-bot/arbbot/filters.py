"""Exchange order-size rules: tick size, step size, minimum notional.

Detection uses floats for speed; anything that turns into a real order goes
through these Decimal helpers so we never send a quantity the venue rejects
(or, worse, silently rounds in a direction that eats the edge).
"""
from __future__ import annotations

from decimal import ROUND_DOWN, Decimal

from .models import Market


def _d(x: float | str | Decimal) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


def round_step(qty: float | Decimal, step: float | Decimal | None) -> Decimal:
    """Round a quantity DOWN to a multiple of `step` (never up: never overspend)."""
    q = _d(qty)
    if not step:
        return q
    s = _d(step)
    if s <= 0:
        return q
    return (q / s).to_integral_value(rounding=ROUND_DOWN) * s


def round_tick(price: float | Decimal, tick: float | Decimal | None, side: str) -> Decimal:
    """Round a price to the tick grid, in the direction that stays marketable.

    Buy prices round up, sell prices round down, so a limit order at the rounded
    price still crosses the book it was meant to cross.
    """
    p = _d(price)
    if not tick:
        return p
    t = _d(tick)
    if t <= 0:
        return p
    n = p / t
    if side == "buy":
        n = n.to_integral_value(rounding="ROUND_CEILING")
    else:
        n = n.to_integral_value(rounding=ROUND_DOWN)
    return n * t


def size_order(market: Market, price: float, qty: float, side: str) -> tuple[Decimal, Decimal, str | None]:
    """Apply a market's filters to (price, qty).

    Returns (price, qty, rejection_reason). qty is rounded down to the step
    size; the result is rejected when it violates min_qty or min_notional.
    """
    if not market.step_size or not market.tick_size:
        return _d(price), _d(qty), "no exchange filters known for this market (run with discovery, not --static)"
    p = round_tick(price, market.tick_size, side)
    q = round_step(qty, market.step_size)
    if q <= 0:
        return p, q, "quantity rounds to zero"
    if market.min_qty is not None and q < _d(market.min_qty):
        return p, q, f"below min_qty {market.min_qty}"
    if market.min_notional is not None and p * q < _d(market.min_notional):
        return p, q, f"below min_notional {market.min_notional}"
    return p, q, None


def parse_binance_filters(symbol_info: dict) -> dict[str, float | None]:
    """Pull tick/step/min values out of a Binance exchangeInfo symbol entry."""
    out: dict[str, float | None] = {"tick_size": None, "step_size": None, "min_qty": None, "min_notional": None}
    for f in symbol_info.get("filters", []):
        ft = f.get("filterType")
        if ft == "PRICE_FILTER":
            out["tick_size"] = float(f.get("tickSize", 0)) or None
        elif ft == "LOT_SIZE":
            out["step_size"] = float(f.get("stepSize", 0)) or None
            out["min_qty"] = float(f.get("minQty", 0)) or None
        elif ft in ("NOTIONAL", "MIN_NOTIONAL"):
            out["min_notional"] = float(f.get("minNotional", 0)) or None
    return out
