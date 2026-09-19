"""Triangular arbitrage inside one venue.

Start with a quote asset (USDT), go USDT -> X -> Y -> USDT across three
markets and see whether more comes back than went out, after three taker
fees. Cycles are enumerated once from the market list and indexed by symbol so
a quote update only re-evaluates the cycles it touches.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from ..config import DetectionConfig
from ..fees import FeeSchedule, leg_net_rate
from ..models import Leg, Market, Opportunity, Quote
from ..quotes import QuoteBook
from .base import Detector


@dataclass(frozen=True)
class Edge:
    """Converting `src` into `dst` through `market`. side is what we do to the market's base."""

    src: str
    dst: str
    market: Market
    side: str  # "buy": dst is the base we buy with src; "sell": src is the base we sell for dst


@dataclass(frozen=True)
class Cycle:
    start: str
    edges: tuple[Edge, Edge, Edge]

    @property
    def path(self) -> str:
        return " -> ".join([self.start] + [e.dst for e in self.edges])


def build_edges(markets: list[Market]) -> dict[str, list[Edge]]:
    by_src: dict[str, list[Edge]] = defaultdict(list)
    for m in markets:
        by_src[m.quote].append(Edge(m.quote, m.base, m, "buy"))
        by_src[m.base].append(Edge(m.base, m.quote, m, "sell"))
    return by_src


def build_cycles(markets: list[Market], start_assets: list[str]) -> list[Cycle]:
    by_src = build_edges(markets)
    cycles: list[Cycle] = []
    seen: set[tuple] = set()
    for start in start_assets:
        for e1 in by_src.get(start, []):
            x = e1.dst
            if x == start:
                continue
            for e2 in by_src.get(x, []):
                y = e2.dst
                if y in (start, x):
                    continue
                for e3 in by_src.get(y, []):
                    if e3.dst != start:
                        continue
                    key = (start, e1.market.symbol, e1.side, e2.market.symbol, e2.side, e3.market.symbol, e3.side)
                    if key in seen:
                        continue
                    seen.add(key)
                    cycles.append(Cycle(start, (e1, e2, e3)))
    return cycles


class TriangularDetector(Detector):
    name = "triangular"

    def __init__(self, markets: list[Market], fees: FeeSchedule, cfg: DetectionConfig, max_notional_usd: float):
        venues = {m.venue for m in markets}
        if len(venues) > 1:
            raise ValueError("TriangularDetector wants markets from a single venue")
        self.venue = next(iter(venues)) if venues else ""
        self.fee = fees.taker(self.venue) if self.venue else 0.0
        self.cfg = cfg
        self.max_notional_usd = float(max_notional_usd)
        self.cycles = build_cycles(markets, cfg.triangle_start_assets)
        self.by_symbol: dict[str, list[Cycle]] = defaultdict(list)
        for c in self.cycles:
            for e in c.edges:
                if c not in self.by_symbol[e.market.symbol]:
                    self.by_symbol[e.market.symbol].append(c)
        self.evaluations = 0

    def on_quote(self, q: Quote, book: QuoteBook, now: float) -> list[Opportunity]:
        if q.venue != self.venue:
            return []
        out: list[Opportunity] = []
        for cycle in self.by_symbol.get(q.symbol, ()):
            self.evaluations += 1
            opp = self.evaluate(cycle, book, now)
            if opp is not None:
                out.append(opp)
        return out

    def evaluate(self, cycle: Cycle, book: QuoteBook, now: float) -> Opportunity | None:
        quotes: list[Quote] = []
        for e in cycle.edges:
            q = book.get(self.venue, e.market.symbol)
            if q is None or not book.is_fresh(q, now):
                return None
            quotes.append(q)
        # Pass 1: rates and the largest start amount the displayed sizes allow.
        gross = 1.0
        net = 1.0
        max_start = float("inf")
        for e, q in zip(cycle.edges, quotes):
            if e.side == "buy":
                rate = 1.0 / q.ask
                cap_in_src = q.ask_qty * q.ask  # displayed base size, in src (quote) units
            else:
                rate = q.bid
                cap_in_src = q.bid_qty  # displayed bid size is in base = src units
            max_start = min(max_start, cap_in_src / net)  # net = cumulative multiplier so far
            gross *= rate
            net *= leg_net_rate(rate, self.fee, e.side)
        gross_bps = (gross - 1.0) * 1e4
        net_bps = (net - 1.0) * 1e4
        if gross_bps <= 0:
            return None
        start_usd = book.usd_price(cycle.start, now)
        if start_usd is None or start_usd <= 0:
            return None
        start_amt = min(max_start, self.max_notional_usd / start_usd)
        if start_amt <= 0:
            return None
        # Pass 2: leg quantities in each market's base units.
        legs: list[Leg] = []
        amt = start_amt
        for e, q in zip(cycle.edges, quotes):
            if e.side == "buy":
                qty = amt / (q.ask * (1.0 + self.fee))
                legs.append(Leg(self.venue, e.market.symbol, "buy", e.market.base, e.market.quote, q.ask, qty, self.fee))
                amt = qty
            else:
                qty = amt
                legs.append(Leg(self.venue, e.market.symbol, "sell", e.market.base, e.market.quote, q.bid, qty, self.fee))
                amt = qty * q.bid * (1.0 - self.fee)
        profit_start_units = amt - start_amt
        return Opportunity(
            kind=self.name,
            ts=now,
            legs=legs,
            gross_edge_bps=gross_bps,
            net_edge_bps=net_bps,
            notional_usd=start_amt * start_usd,
            expected_profit_usd=profit_start_units * start_usd,
            description=f"{self.venue} {cycle.path}: " + ", ".join(
                f"{e.side} {e.market.symbol} @ {(q.ask if e.side == 'buy' else q.bid):g}" for e, q in zip(cycle.edges, quotes)),
            quote_ages_ms=[q.age_ms(now) for q in quotes],
            extra={"start": cycle.start, "start_amount": start_amt, "fee_bps": self.fee * 1e4},
        )
