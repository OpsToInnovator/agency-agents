"""QuoteBook: the latest top-of-book for every market, indexed for detectors."""
from __future__ import annotations

import logging
from collections import defaultdict
from statistics import median

from .models import BINANCE, USD_FAMILY, Market, Quote

log = logging.getLogger(__name__)


CHANGE_NONE = 0  # same top of book as before (Coinbase resends it on every trade)
CHANGE_SIZE = 1  # only displayed sizes moved
CHANGE_PRICE = 2  # best bid or ask price moved
CHANGE_NEW = 3  # first quote for this market (or first after invalidation)


class QuoteBook:
    def __init__(self, max_age_s: float = 2.0, max_age_by_venue: dict[str, float] | None = None,
                 anchor_venue: str = BINANCE, stable_rate_band: tuple[float, float] = (0.97, 1.03)):
        self.max_age_s = float(max_age_s)
        self.max_age_by_venue = {k.lower(): float(v) for k, v in (max_age_by_venue or {}).items()}
        self.anchor_venue = anchor_venue.lower()
        self.stable_rate_band = (float(stable_rate_band[0]), float(stable_rate_band[1]))
        self.stable_rate_max_age_s = 600.0  # USDT/USD moves slowly; a 10-minute-old print beats assuming par
        self.stable_rate_rejections = 0
        self.quarantined: dict[str, str] = {}  # base asset -> reason
        self.markets: dict[tuple[str, str], Market] = {}
        self._by_key: dict[tuple[str, str], Quote] = {}
        self._by_venue: dict[str, dict[str, Quote]] = defaultdict(dict)
        # base asset -> {(venue, symbol): Quote} for markets quoted in a USD-family asset
        self._usd_by_base: dict[str, dict[tuple[str, str], Quote]] = defaultdict(dict)
        # USDT-USD style quotes per venue: the USD value of one unit of a stablecoin
        self._stable_quotes: dict[str, dict[str, Quote]] = defaultdict(dict)
        self.updates = 0

    # -- registration -----------------------------------------------------
    def register(self, market: Market) -> None:
        self.markets[market.key] = market

    def market(self, venue: str, symbol: str) -> Market | None:
        return self.markets.get((venue, symbol))

    # -- updates ----------------------------------------------------------
    def update(self, q: Quote) -> int:
        """Store a quote; returns what changed versus the previous one (CHANGE_*)."""
        prev = self._by_key.get(q.key)
        self._by_key[q.key] = q
        self._by_venue[q.venue][q.symbol] = q
        if q.quote in USD_FAMILY:
            self._usd_by_base[q.base][q.key] = q
            if q.base in USD_FAMILY and q.quote == "USD" and q.is_sane:
                # e.g. Coinbase USDT-USD: how many dollars one USDT is worth
                lo, hi = self.stable_rate_band
                if lo <= q.mid <= hi:
                    self._stable_quotes[q.base][q.venue] = q
                else:
                    self.stable_rate_rejections += 1
        self.updates += 1
        if prev is None:
            return CHANGE_NEW
        if prev.bid != q.bid or prev.ask != q.ask:
            return CHANGE_PRICE
        if prev.bid_qty != q.bid_qty or prev.ask_qty != q.ask_qty:
            return CHANGE_SIZE
        return CHANGE_NONE

    # -- lookups ----------------------------------------------------------
    def get(self, venue: str, symbol: str) -> Quote | None:
        return self._by_key.get((venue, symbol))

    def is_fresh(self, q: Quote, now: float) -> bool:
        return q.is_sane and (now - q.recv_ts) <= self.max_age_by_venue.get(q.venue, self.max_age_s)

    def invalidate_venue(self, venue: str) -> int:
        """Forget every quote from a venue (its connection dropped)."""
        keys = [k for k in self._by_key if k[0] == venue]
        for k in keys:
            q = self._by_key.pop(k)
            self._by_venue.get(venue, {}).pop(q.symbol, None)
            self._usd_by_base.get(q.base, {}).pop(k, None)
        for per_venue in self._stable_quotes.values():
            per_venue.pop(venue, None)
        return len(keys)

    def quarantine(self, base: str, reason: str) -> bool:
        """Exclude `base` from cross-venue comparison. Returns True the first time."""
        if base in self.quarantined:
            return False
        self.quarantined[base] = reason
        log.warning("QUARANTINE %s: %s", base, reason)
        return True

    def venue_quotes(self, venue: str) -> dict[str, Quote]:
        return self._by_venue.get(venue, {})

    def usd_rate(self, asset: str, now: float | None = None) -> float | None:
        """Dollars per unit of a USD-family asset (1.0 for USD, ~0.9996 for USDT):
        the median of the venues' recent USDT-USD mids, or par when none is recent."""
        if asset == "USD":
            return 1.0
        if asset in USD_FAMILY:
            mids = [q.mid for q in self._stable_quotes.get(asset, {}).values()
                    if now is None or now - q.recv_ts <= self.stable_rate_max_age_s]
            return float(median(mids)) if mids else 1.0
        return None

    def usd_quotes_for_base(self, base: str, now: float) -> list[Quote]:
        """Fresh USD-family quotes for `base` across all venues (anchor venue
        only once the asset is quarantined)."""
        quotes = [q for q in self._usd_by_base.get(base, {}).values() if self.is_fresh(q, now)]
        if base in self.quarantined:
            quotes = [q for q in quotes if q.venue == self.anchor_venue]
        return quotes

    def usd_price(self, asset: str, now: float, _depth: int = 0) -> float | None:
        """Best-effort USD mark for an asset: median of fresh USD-family mids,
        else via a BTC or ETH cross, else the stable rate, else None."""
        if asset in USD_FAMILY:
            return self.usd_rate(asset, now)
        quotes = self.usd_quotes_for_base(asset, now)
        mids = [q.mid * (self.usd_rate(q.quote, now) or 1.0) for q in quotes]
        if mids:
            return float(median(mids))
        if _depth > 0:
            return None
        for hub in ("BTC", "ETH"):
            hub_usd = self.usd_price(hub, now, _depth=1)
            if hub_usd is None:
                continue
            for venue, symbols in self._by_venue.items():
                for q in symbols.values():
                    if q.base == asset and q.quote == hub and self.is_fresh(q, now):
                        return q.mid * hub_usd
        return None

    def count(self) -> int:
        return len(self._by_key)

    def stale_count(self, now: float) -> int:
        return sum(1 for q in self._by_key.values() if not self.is_fresh(q, now))

    def all_quotes(self) -> list[Quote]:
        return list(self._by_key.values())
