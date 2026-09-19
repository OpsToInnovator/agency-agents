"""Paper executor: simulated fills against the quotes we saw.

Two fill models:

* ``arrival`` (default): the order reaches the venue ``assumed_rtt_ms`` after
  detection and is an IOC limit at the detection price against the book at
  THAT time. If the touch moved away, the leg is missed. Cross-exchange legs
  travel in parallel; a triangle's legs are sequential (leg k+1 is sized from
  what leg k actually delivered). This is what turns "the scanner saw +3 bps"
  into "the order would have arrived at -1 bps", the latency tax.
* ``instant``: fills at detection, all legs at once, the generous model.

Both: fees charged in the quote asset at the venue's taker rate, sizes capped
by displayed top-of-book size times ``fill_fraction``, ``slippage_bps`` on top,
inventory on every venue funded lazily at the first mark. Real fills are
worse than either model. If paper mode is not profitable, live will not be.
"""
from __future__ import annotations

import copy
from collections import defaultdict
from dataclasses import dataclass, field

from ..config import PaperConfig
from ..fees import FeeSchedule
from ..models import BINANCE, USD_FAMILY, Fill, Leg, Opportunity, TradeRecord
from ..quotes import QuoteBook

_MIN_NOTIONAL_USD = 1.0


@dataclass
class _Plan:
    opp: Opportunity
    created: float
    legs: list[Leg]
    sequential: bool
    due: list[float]
    resolved: list[bool]
    fills: list[Fill] = field(default_factory=list)
    missed: list[str] = field(default_factory=list)
    deltas: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    carry: float | None = None  # sequential plans: what the previous leg delivered
    scale: float = 1.0


class PaperExecutor:
    remote = False  # executes in-process; the engine awaits it inline

    @staticmethod
    def can_execute(opp: Opportunity) -> bool:
        return opp.executable

    def __init__(self, cfg: PaperConfig, fees: FeeSchedule, book: QuoteBook, venues: list[str]):
        self.cfg = cfg
        self.fees = fees
        self.book = book
        self.balances: dict[str, dict[str, float]] = {v: {} for v in venues}
        # what was put in, per (venue, asset), so it can be marked with the same marks as equity
        self.contributed: dict[tuple[str, str], float] = defaultdict(float)
        self.realized_pnl_usd = 0.0
        self.promised_pnl_usd = 0.0  # what the detector expected for the trades that settled
        self.trades = 0
        self.rejected = 0
        self.missed_legs = 0
        self.pending: list[_Plan] = []
        # displayed size already taken by other plans that settled against the same quote
        self._consumed: dict[tuple[str, str, str, float], float] = {}
        for v in venues:
            quote = "USDT" if v == BINANCE else "USD"
            self.balances[v][quote] = cfg.starting_quote_per_venue_usd
            self.contributed[(v, quote)] += cfg.starting_quote_per_venue_usd

    # -- balances ---------------------------------------------------------
    def balance(self, venue: str, asset: str) -> float:
        return self.balances.get(venue, {}).get(asset, 0.0)

    def contributions_value_usd(self, now: float) -> float:
        """Everything that was put in, marked at the same marks equity uses, so
        equity - contributions == realized + unrealized exactly, and a book with no
        trades shows zero PnL even when USDT is not worth a dollar."""
        total = 0.0
        for (venue, asset), qty in self.contributed.items():
            mark = self._mark(asset, now)
            total += qty * (mark if mark is not None else 0.0)
        return total

    @property
    def latency_tax_usd(self) -> float:
        """Promised minus realized over settled trades: what waiting for the order to arrive cost."""
        return self.promised_pnl_usd - self.realized_pnl_usd

    def _ensure_inventory(self, venue: str, asset: str, now: float) -> None:
        """Fund a base asset the first time a venue needs to sell it (once per
        (venue, asset), tracked in the contributions ledger; a holding created by an
        earlier buy is topped up, not overwritten). With no mark available yet the
        asset stays unfunded (not pinned) so a later call can fund it."""
        self.balances.setdefault(venue, {})
        if (venue, asset) in self.contributed or self.cfg.starting_base_inventory_usd <= 0:
            return
        mark = self.book.usd_price(asset, now)
        if mark is None or mark <= 0:
            return
        qty = self.cfg.starting_base_inventory_usd / mark
        self.balances[venue][asset] = self.balances[venue].get(asset, 0.0) + qty
        self.contributed[(venue, asset)] += qty

    def _fundable(self, opp: Opportunity) -> str | None:
        """Reason a plan cannot be sent from the balances on hand, else None. Legs
        that consume what an earlier leg of the same plan produces are fine."""
        produced: set[tuple[str, str]] = set()
        for i, leg in enumerate(opp.legs):
            need = (leg.venue, leg.base) if leg.side == "sell" else (leg.venue, leg.quote)
            if need not in produced and self.balance(*need) <= 0:
                return f"leg{i + 1} {leg.symbol}: no {need[1]} on {need[0]}"
            produced.add((leg.venue, leg.quote if leg.side == "sell" else leg.base))
        return None

    def _fund_for(self, opp: Opportunity, now: float) -> None:
        # Sell legs need inventory unless an earlier leg of the same plan produces the
        # asset (a triangle sells what it just bought; a cross-exchange trade sells from stock).
        produced: set[tuple[str, str]] = set()
        for leg in opp.legs:
            if leg.side == "sell":
                if (leg.venue, leg.base) not in produced:
                    self._ensure_inventory(leg.venue, leg.base, now)
                produced.add((leg.venue, leg.quote))
            else:
                produced.add((leg.venue, leg.base))

    def _mark(self, asset: str, now: float) -> float | None:
        if asset in USD_FAMILY:
            return self.book.usd_rate(asset, now)
        return self.book.usd_price(asset, now)

    def equity_usd(self, now: float) -> tuple[float, list[str]]:
        """Portfolio value and the holdings that could not be marked. A contributed
        asset counts as unmarked even at zero quantity, so equity and
        contributions_value_usd can never disagree about what is priceable."""
        total = 0.0
        unmarked: list[str] = []
        for venue, assets in self.balances.items():
            for asset, qty in assets.items():
                if qty == 0 and (venue, asset) not in self.contributed:
                    continue
                mark = self._mark(asset, now)
                if mark is None:
                    unmarked.append(f"{venue}:{asset}")
                    continue
                total += qty * mark
        return total, unmarked

    # -- entry point ------------------------------------------------------
    async def execute(self, opp: Opportunity, now: float) -> TradeRecord:
        self._fund_for(opp, now)
        if self.cfg.fill_model == "instant":
            return self._execute_instant(opp, now)
        reason = self._fundable(opp)
        if reason:
            self.rejected += 1
            return TradeRecord(opp, [], "rejected", f"insufficient paper balance: {reason}", 0.0, now)
        sequential = opp.kind == "triangular"
        rtt = self.cfg.assumed_rtt_ms / 1000.0
        due = [now + rtt] * len(opp.legs) if not sequential else [now + rtt] + [float("inf")] * (len(opp.legs) - 1)
        plan = _Plan(opp, now, list(opp.legs), sequential, due, [False] * len(opp.legs))
        self.pending.append(plan)
        return TradeRecord(opp, [], "pending", f"arrival model: legs due in {self.cfg.assumed_rtt_ms:g} ms", 0.0, now,
                           promised_pnl_usd=opp.expected_profit_usd)

    # -- arrival model ----------------------------------------------------
    def settle(self, now: float, final: bool = False) -> list[TradeRecord]:
        """Resolve legs whose orders have 'arrived'. Called on every quote update
        (and once more with final=True at shutdown/end of tape)."""
        done: list[TradeRecord] = []
        if not self.pending:
            self._consumed.clear()  # nothing in flight: forget old quotes' consumption
        for plan in list(self.pending):
            progressed = True
            while progressed:
                progressed = False
                for i, leg in enumerate(plan.legs):
                    if plan.resolved[i]:
                        continue
                    if plan.sequential and i > 0 and not plan.resolved[i - 1]:
                        break
                    if plan.due[i] - now > 1e-6 and not final:
                        continue
                    self._settle_leg(plan, i, now)
                    progressed = True
                    if plan.sequential and i + 1 < len(plan.legs):
                        if plan.missed and plan.missed[-1].startswith(f"leg{i + 1}"):
                            for j in range(i + 1, len(plan.legs)):
                                plan.resolved[j] = True
                                plan.missed.append(f"leg{j + 1} {plan.legs[j].symbol}: not sent (cycle aborted)")
                        else:
                            plan.due[i + 1] = now + self.cfg.assumed_rtt_ms / 1000.0
            if all(plan.resolved):
                self.pending.remove(plan)
                done.append(self._finish(plan, now))
        return done

    def _settle_leg(self, plan: _Plan, i: int, now: float) -> None:
        leg = plan.legs[i]
        plan.resolved[i] = True
        q = self.book.get(leg.venue, leg.symbol)
        fee_rate = self.fees.taker(leg.venue)
        slip = self.cfg.slippage_bps / 1e4
        acct = self.balances.setdefault(leg.venue, {})
        if q is None or not q.is_sane:
            plan.missed.append(f"leg{i + 1} {leg.symbol}: no quote at arrival")
            self.missed_legs += 1
            return
        # planned size, in this market's base units
        if plan.sequential and i > 0 and plan.carry is not None:
            # previous leg delivered `carry` units of this leg's input asset
            if leg.side == "buy":
                want = plan.carry / (q.ask * (1.0 + fee_rate))
            else:
                want = plan.carry
        else:
            want = leg.qty * plan.scale
        depth_key = (leg.venue, leg.symbol, leg.side, q.recv_ts)
        taken = self._consumed.get(depth_key, 0.0)
        if leg.side == "buy":
            if q.ask > leg.price:
                plan.missed.append(f"leg{i + 1} {leg.symbol}: ask moved {leg.price:g} -> {q.ask:g}")
                self.missed_legs += 1
                plan.carry = 0.0
                return
            price = q.ask * (1.0 + slip)
            qty = min(want, max(0.0, q.ask_qty * self.cfg.fill_fraction - taken))
            have = acct.get(leg.quote, 0.0)
            cost = qty * price * (1.0 + fee_rate)
            if cost > have:
                qty = have / (price * (1.0 + fee_rate)) if have > 0 else 0.0
                cost = qty * price * (1.0 + fee_rate)
            if qty <= 0 or qty * price < _MIN_NOTIONAL_USD * 1e-3:
                plan.missed.append(f"leg{i + 1} {leg.symbol}: no size (displayed {q.ask_qty:g}, balance {have:g} {leg.quote})")
                self.missed_legs += 1
                plan.carry = 0.0
                return
            acct[leg.quote] = have - cost
            acct[leg.base] = acct.get(leg.base, 0.0) + qty
            plan.deltas[leg.quote] -= cost
            plan.deltas[leg.base] += qty
            plan.fills.append(Fill(leg.venue, leg.symbol, "buy", price, qty, qty * price * fee_rate, leg.quote, now, "paper"))
            plan.carry = qty
            self._consumed[depth_key] = taken + qty
        else:
            if q.bid < leg.price:
                plan.missed.append(f"leg{i + 1} {leg.symbol}: bid moved {leg.price:g} -> {q.bid:g}")
                self.missed_legs += 1
                plan.carry = 0.0
                return
            price = q.bid * (1.0 - slip)
            qty = min(want, max(0.0, q.bid_qty * self.cfg.fill_fraction - taken))
            have = acct.get(leg.base, 0.0)
            if qty > have:
                qty = have
            if qty <= 0 or qty * price < _MIN_NOTIONAL_USD * 1e-3:
                plan.missed.append(f"leg{i + 1} {leg.symbol}: no size (displayed {q.bid_qty:g}, balance {have:g} {leg.base})")
                self.missed_legs += 1
                plan.carry = 0.0
                return
            proceeds = qty * price * (1.0 - fee_rate)
            acct[leg.base] = have - qty
            acct[leg.quote] = acct.get(leg.quote, 0.0) + proceeds
            plan.deltas[leg.base] -= qty
            plan.deltas[leg.quote] += proceeds
            plan.fills.append(Fill(leg.venue, leg.symbol, "sell", price, qty, qty * price * fee_rate, leg.quote, now, "paper"))
            plan.carry = proceeds
            self._consumed[depth_key] = taken + qty

    def _finish(self, plan: _Plan, now: float) -> TradeRecord:
        realized = 0.0
        for asset, d in plan.deltas.items():
            if abs(d) < 1e-15:
                continue
            mark = self._mark(asset, now)
            realized += d * (mark if mark is not None else 0.0)  # unmarkable residue counts as worthless
        if not plan.fills:
            status = "missed"
            self.rejected += 1
        else:
            status = "filled" if not plan.missed else "partial"
            self.trades += 1
            self.realized_pnl_usd += realized
            self.promised_pnl_usd += plan.opp.expected_profit_usd * plan.scale
        reason = "; ".join(plan.missed) if plan.missed else ""
        return TradeRecord(plan.opp, plan.fills, status, reason, realized if plan.fills else 0.0, now,
                           promised_pnl_usd=plan.opp.expected_profit_usd * plan.scale if plan.fills else 0.0,
                           latency_ms=max(0.0, (now - plan.created) * 1e3))

    # -- instant model ----------------------------------------------------
    def _execute_instant(self, opp: Opportunity, now: float) -> TradeRecord:
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
        does not fit and returns how much of the plan would have fit. A leg that
        consumes what an earlier leg of the same plan produced is sized from that
        amount (slippage and fees shrink it), never from the detector's plan."""
        balances = copy.deepcopy(self.balances)
        fills: list[Fill] = []
        slip = self.cfg.slippage_bps / 1e4
        produced: dict[tuple[str, str], float] = {}
        for leg in opp.legs:
            qty = leg.qty * self.cfg.fill_fraction * scale
            fee_rate = self.fees.taker(leg.venue)
            acct = balances.setdefault(leg.venue, {})
            if leg.side == "buy":
                price = leg.price * (1.0 + slip)
                if (leg.venue, leg.quote) in produced:
                    qty = min(qty, produced[(leg.venue, leg.quote)] / (price * (1.0 + fee_rate)))
                cost = qty * price * (1.0 + fee_rate)
                have = acct.get(leg.quote, 0.0)
                if cost > have:
                    if have <= 0:
                        return None
                    return fills, balances, have / cost, False
                acct[leg.quote] = have - cost
                acct[leg.base] = acct.get(leg.base, 0.0) + qty
                produced[(leg.venue, leg.base)] = produced.get((leg.venue, leg.base), 0.0) + qty
                fills.append(Fill(leg.venue, leg.symbol, "buy", price, qty, qty * price * fee_rate, leg.quote, now, "paper"))
            else:
                price = leg.price * (1.0 - slip)
                if (leg.venue, leg.base) in produced:
                    qty = min(qty, produced[(leg.venue, leg.base)])
                have = acct.get(leg.base, 0.0)
                if qty > have:
                    if have <= 0:
                        return None
                    return fills, balances, have / qty, False
                proceeds = qty * price * (1.0 - fee_rate)
                acct[leg.base] = have - qty
                acct[leg.quote] = acct.get(leg.quote, 0.0) + proceeds
                produced[(leg.venue, leg.quote)] = produced.get((leg.venue, leg.quote), 0.0) + proceeds
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
            mark = self._mark(asset, now)
            realized += d * (mark if mark is not None else 0.0)
        self.balances = balances
        self.realized_pnl_usd += realized
        promised = opp.expected_profit_usd * scale * self.cfg.fill_fraction
        self.promised_pnl_usd += promised
        self.trades += 1
        status = "filled" if scale >= 0.999 and self.cfg.fill_fraction >= 0.999 else "partial"
        return TradeRecord(opp, fills, status, f"scale={scale:.3f}" if status == "partial" else "", realized, now,
                           promised_pnl_usd=promised)
