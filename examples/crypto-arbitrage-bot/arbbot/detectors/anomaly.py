"""Quote anomalies: the "price errors" the viral post talks about.

Three checks, all report-only (an anomaly that is genuinely tradable shows up
as a cross-exchange opportunity anyway):
  * crossed_book  - bid >= ask on one venue: bad data, never a free lunch
  * cross_venue   - a venue's mid is far from the median of the other venues
  * jump          - a venue's mid jumped far from its own recent EWMA
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import median

from ..config import DetectionConfig
from ..models import USD_FAMILY, Opportunity, Quote
from ..quotes import QuoteBook
from .base import Detector


@dataclass
class _State:
    ewma: float
    ts: float


class AnomalyDetector(Detector):
    name = "anomaly"

    def __init__(self, cfg: DetectionConfig):
        self.cfg = cfg
        self._state: dict[tuple[str, str], _State] = {}
        self._last_flag: dict[tuple[str, str, str], float] = {}
        self.flags = 0

    def _flag(self, q: Quote, subtype: str, deviation_bps: float, now: float, reference: float | None) -> Opportunity | None:
        key = (q.venue, q.symbol, subtype)
        last = self._last_flag.get(key)
        if last is not None and now - last < self.cfg.anomaly_cooldown_s:
            return None
        self._last_flag[key] = now
        self.flags += 1
        return Opportunity(
            kind=self.name,
            ts=now,
            legs=[],
            gross_edge_bps=abs(deviation_bps),
            net_edge_bps=0.0,
            notional_usd=0.0,
            expected_profit_usd=0.0,
            description=f"{q.venue} {q.symbol} {subtype}: {deviation_bps:+.1f} bps"
            + (f" vs reference {reference:g}" if reference else ""),
            quote_ages_ms=[q.age_ms(now)],
            extra={"subtype": subtype, "deviation_bps": deviation_bps, "reference": reference,
                   "bid": q.bid, "ask": q.ask},
        )

    def on_quote(self, q: Quote, book: QuoteBook, now: float) -> list[Opportunity]:
        out: list[Opportunity] = []
        if q.bid <= 0 or q.ask <= 0:
            return out
        if q.bid >= q.ask:
            opp = self._flag(q, "crossed_book", (q.bid / q.ask - 1.0) * 1e4, now, None)
            if opp:
                out.append(opp)
            return out  # do not feed garbage into the EWMA
        mid = q.mid
        # 1. jump versus own recent history
        st = self._state.get(q.key)
        if st is None:
            self._state[q.key] = _State(mid, now)
        else:
            dev = (mid / st.ewma - 1.0) * 1e4
            if abs(dev) >= self.cfg.anomaly_threshold_bps:
                opp = self._flag(q, "jump", dev, now, st.ewma)
                if opp:
                    out.append(opp)
            dt = max(0.0, now - st.ts)
            alpha = 1.0 - math.exp(-dt * math.log(2) / max(1e-3, self.cfg.anomaly_ewma_halflife_s))
            st.ewma += alpha * (mid - st.ewma)
            st.ts = now
        # 2. deviation from the other venues
        if q.quote in USD_FAMILY and q.base not in USD_FAMILY:
            others = [o for o in book.usd_quotes_for_base(q.base, now) if o.venue != q.venue]
            if others:
                ref = median(o.mid * (book.usd_rate(o.quote) or 1.0) for o in others)
                mine = mid * (book.usd_rate(q.quote) or 1.0)
                dev = (mine / ref - 1.0) * 1e4
                if abs(dev) >= self.cfg.anomaly_threshold_bps:
                    opp = self._flag(q, "cross_venue", dev, now, ref)
                    if opp:
                        out.append(opp)
        return out
