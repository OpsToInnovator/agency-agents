"""Paper executor: simulated fills against the quotes we saw.

Assumptions, all of them generous to the bot:
  * we get the displayed top-of-book size (times `fill_fraction`) at the
    quoted price plus `slippage_bps`, on every leg, simultaneously;
  * fees are charged in the quote asset at the venue's taker rate;
  * inventory already sits on every venue (funded lazily at the first mark),
    so no transfers are needed.
Real fills are worse than this. If paper mode is not profitable, live will not be.
"""
from __future__ import annotations

import copy
from collections import defaultdict

from ..config import PaperConfig
from ..fees import FeeSchedule
from ..models import BINANCE, USD_FAMILY, Fill, Opportunity, TradeRecord
from ..quotes import QuoteBook

_MIN_NOTIONAL_USD = 1.0


class PaperExecutor:
    def __init__(self, cfg: PaperConfig, fees: FeeSchedule, book: QuoteBook, venues: list[str]):
        self.cfg = cfg
        self.fees = fees
        self.book = book
        self.balances: dict[str, dict[str, float]] = {v: {} for v in venues}
        self.contributions_usd = 0.0
        self.realized_pnl_usd = 0.0
        self.trades = 0
        self.rejected = 0
        for v in venues:
            quote = "USDT" if v == BINANCE else "USD"
            self.balances[v][quote] = cfg.starting_quote_per_venue_usd
            self.contributions_usd += cfg.starting_quote_per_venue_usd  # stablecoins counted at par at funding

    # -- balances ---------------------------------------------------------
    def balance(self, venue: str, asset: str) -> float:
        return self.balances.get(venue, {}).get(asset, 0.0)

    def _ensure_inventory(self, venue: str, asset: str, now: float) -> None:
        """Fund a base asset the first time a venue needs to sell it."""
        if asset in self.balances.setdefault(venue, {}):
            return
        mark = self.book.usd_price(asset, now)
        if mark is None or mark <= 0 or self.cfg.starting_base_inventory_usd <= 0:
            self.balances[venue][asset] = 0.0
            return
        qty = self.cfg.starting_base_inventory_usd / mark
        self.balances[venue][asset] = qty
        self.contributions_usd += self.cfg.starting_base_inventory_usd

    def _mark(self, asset: str, now: float) -> float | None:
        return self.book.usd_price(asset, now)

    def equity_usd(self, now: float) -> tuple[float, list[str]]:
        total = 0.0
        unmarked: list[str] = []
        for venue, assets in self.balances.items():
            for asset, qty in assets.items():
                if qty == 0:
                    continue
                mark = self._mark(asset, now)
                if mark is None:
                    unmarked.append(f"{venue}:{asset}")
                    continue
                total += qty * mark
        return total, unmarked

    # -- execution --------------------------------------------------------
    async def execute(self, opp: Opportunity, now: float) -> TradeRecord:
        # Sell legs need inventory unless an earlier leg of the same plan produces it
        # (a triangle sells what it just bought; a cross-exchange trade sells from stock).
        produced: set[tuple[str, str]] = set()
        for leg in opp.legs:
            if leg.side == "sell" and (leg.venue, leg.base) not in produced:
                self._ensure_inventory(leg.venue, leg.base, now)
            elif leg.side == "buy":
                produced.add((leg.venue, leg.base))
        # All-or-nothing: every leg must fit the paper balances at the same
        # scale, because later legs consume what earlier legs produce.
        scale = 1.0
        for _ in range(6):
            result = self._simulate(opp, scale, now)
            if result is None:
                break
            fills, balances, feasible, complete = result
            if complete:
                return self._commit(opp, fills, balances, scale, now)
            scale *= feasible * (1.0 - 1e-9)  # nudge below the exact limit to beat float rounding
            if scale < 1e-6:
                break
        self.rejected += 1
        return TradeRecord(opp, [], "rejected", "insufficient paper balance", 0.0, now)

    def _simulate(self, opp: Opportunity, scale: float, now: float):
        """Walk the legs on a copy of the balances. Stops at the first leg that
        does not fit and returns how much of the plan would have fit."""
        balances = copy.deepcopy(self.balances)
        fills: list[Fill] = []
        slip = self.cfg.slippage_bps / 1e4
        for leg in opp.legs:
            qty = leg.qty * self.cfg.fill_fraction * scale
            fee_rate = self.fees.taker(leg.venue)
            acct = balances.setdefault(leg.venue, {})
            if leg.side == "buy":
                price = leg.price * (1.0 + slip)
                cost = qty * price * (1.0 + fee_rate)
                have = acct.get(leg.quote, 0.0)
                if cost > have:
                    if have <= 0:
                        return None
                    return fills, balances, have / cost, False
                acct[leg.quote] = have - cost
                acct[leg.base] = acct.get(leg.base, 0.0) + qty
                fills.append(Fill(leg.venue, leg.symbol, "buy", price, qty, qty * price * fee_rate, leg.quote, now, "paper"))
            else:
                price = leg.price * (1.0 - slip)
                have = acct.get(leg.base, 0.0)
                if qty > have:
                    if have <= 0:
                        return None
                    return fills, balances, have / qty, False
                proceeds = qty * price * (1.0 - fee_rate)
                acct[leg.base] = have - qty
                acct[leg.quote] = acct.get(leg.quote, 0.0) + proceeds
                fills.append(Fill(leg.venue, leg.symbol, "sell", price, qty, qty * price * fee_rate, leg.quote, now, "paper"))
        return fills, balances, 1.0, True

    def _commit(self, opp: Opportunity, fills: list[Fill], balances: dict, scale: float, now: float) -> TradeRecord:
        notional_usd = opp.notional_usd * scale * self.cfg.fill_fraction
        if notional_usd < _MIN_NOTIONAL_USD:
            self.rejected += 1
            return TradeRecord(opp, [], "rejected", f"scaled notional {notional_usd:.4f} USD too small", 0.0, now)
        deltas: dict[str, float] = defaultdict(float)
        for venue, assets in balances.items():
            for asset, qty in assets.items():
                deltas[asset] += qty - self.balances.get(venue, {}).get(asset, 0.0)
        realized = 0.0
        for asset, d in deltas.items():
            if abs(d) < 1e-15:
                continue
            mark = self._mark(asset, now) if asset not in USD_FAMILY else self.book.usd_rate(asset)
            if mark is None:
                mark = 0.0  # cannot mark residual inventory: count it as worthless (conservative)
            realized += d * mark
        self.balances = balances
        self.realized_pnl_usd += realized
        self.trades += 1
        status = "filled" if scale >= 0.999 and self.cfg.fill_fraction >= 0.999 else "partial"
        return TradeRecord(opp, fills, status, f"scale={scale:.3f}" if status == "partial" else "", realized, now)
