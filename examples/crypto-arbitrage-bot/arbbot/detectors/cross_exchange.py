"""Cross-exchange: the same asset priced differently on two venues.

Buy at the cheaper venue's ask, sell at the dearer venue's bid, both at the
same time, out of inventory already held on each venue (no coin transfers:
those take minutes to hours, the spread lasts milliseconds). Sizes are capped
by the displayed top-of-book quantity on both sides and by the per-trade
notional limit. Binance quotes in USDT are converted to USD with the live
USDT-USD rate and charged the configured stablecoin haircut.
"""
from __future__ import annotations

from ..config import DetectionConfig
from ..fees import FeeSchedule, cross_edge
from ..models import USD_FAMILY, Leg, Opportunity, Quote
from ..quotes import QuoteBook
from .base import Detector


class CrossExchangeDetector(Detector):
    name = "cross_exchange"

    def __init__(self, fees: FeeSchedule, cfg: DetectionConfig, max_notional_usd: float):
        self.fees = fees
        self.cfg = cfg
        self.max_notional_usd = float(max_notional_usd)
        self.evaluations = 0

    def on_quote(self, q: Quote, book: QuoteBook, now: float) -> list[Opportunity]:
        if q.quote not in USD_FAMILY or q.base in USD_FAMILY or not book.is_fresh(q, now):
            return []
        if q.base in book.quarantined:
            return []
        quotes = book.usd_quotes_for_base(q.base, now)
        if len(quotes) < 2:
            return []
        out: list[Opportunity] = []
        for other in quotes:
            if other.venue == q.venue:
                continue  # same venue, different stablecoin quote: not a cross-exchange trade
            mismatch = self.identity_check(q, other, book, now)
            if mismatch is not None:
                return [mismatch]
            for buy, sell in ((q, other), (other, q)):
                self.evaluations += 1
                opp = self.evaluate(buy, sell, book, now)
                if opp is not None:
                    out.append(opp)
        return out

    def identity_check(self, a: Quote, b: Quote, book: QuoteBook, now: float) -> Opportunity | None:
        """Two venues disagreeing by more than identity_mismatch_bps are not
        quoting the same asset. Quarantine it and report once."""
        mid_a = a.mid * (book.usd_rate(a.quote) or 1.0)
        mid_b = b.mid * (book.usd_rate(b.quote) or 1.0)
        if mid_a <= 0 or mid_b <= 0:
            return None
        gap_bps = abs(mid_a / mid_b - 1.0) * 1e4
        if gap_bps < self.cfg.identity_mismatch_bps:
            return None
        reason = (f"{a.venue} {a.symbol} mid {a.mid:g} vs {b.venue} {b.symbol} mid {b.mid:g} "
                  f"({gap_bps:,.0f} bps apart): ticker collision, not a price error")
        if not book.quarantine(a.base, reason):
            return None
        return Opportunity(
            kind="anomaly", ts=now, legs=[], gross_edge_bps=gap_bps, net_edge_bps=0.0, notional_usd=0.0,
            expected_profit_usd=0.0, description=f"{a.base} identity_mismatch: {reason}",
            quote_ages_ms=[a.age_ms(now), b.age_ms(now)],
            extra={"subtype": "identity_mismatch", "deviation_bps": gap_bps, "venues": [a.venue, b.venue]},
        )

    def evaluate(self, buy: Quote, sell: Quote, book: QuoteBook, now: float) -> Opportunity | None:
        rate_buy = book.usd_rate(buy.quote) or 1.0
        rate_sell = book.usd_rate(sell.quote) or 1.0
        ask_usd = buy.ask * rate_buy
        bid_usd = sell.bid * rate_sell
        fee_buy = self.fees.taker(buy.venue)
        fee_sell = self.fees.taker(sell.venue)
        gross_bps, net_bps, net_per_unit = cross_edge(ask_usd, fee_buy, bid_usd, fee_sell)
        if gross_bps <= 0:
            return None
        haircut_bps = self.cfg.stable_haircut_bps if buy.quote != sell.quote else 0.0
        if haircut_bps:
            net_bps -= haircut_bps
            net_per_unit -= ask_usd * haircut_bps / 1e4
        qty = min(buy.ask_qty, sell.bid_qty, self.max_notional_usd / ask_usd)
        if qty <= 0:
            return None
        legs = [
            Leg(buy.venue, buy.symbol, "buy", buy.base, buy.quote, buy.ask, qty, fee_buy),
            Leg(sell.venue, sell.symbol, "sell", sell.base, sell.quote, sell.bid, qty, fee_sell),
        ]
        return Opportunity(
            kind=self.name,
            ts=now,
            legs=legs,
            gross_edge_bps=gross_bps,
            net_edge_bps=net_bps,
            notional_usd=qty * ask_usd,
            expected_profit_usd=qty * net_per_unit,
            description=(f"{buy.base}: buy {buy.venue} {buy.symbol} @ {buy.ask:g}, "
                         f"sell {sell.venue} {sell.symbol} @ {sell.bid:g}"),
            quote_ages_ms=[buy.age_ms(now), sell.age_ms(now)],
            extra={"haircut_bps": haircut_bps, "usd_rate_buy": rate_buy, "usd_rate_sell": rate_sell,
                   "fee_bps": [fee_buy * 1e4, fee_sell * 1e4],
                   # trade-triggered feeds (Coinbase ticker) can be seconds behind the real book
                   "exch_lag_ms": [None if q.exch_ts is None else round((q.recv_ts - q.exch_ts) * 1e3, 1)
                                   for q in (buy, sell)]},
        )
