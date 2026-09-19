"""Core data types shared by feeds, detectors and executors."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Assets we treat as interchangeable "dollars" for cross-venue comparison.
# Binance quotes in USDT, Coinbase and Kraken in USD. A configurable haircut
# is charged when an opportunity crosses between two different members.
USD_FAMILY = frozenset({"USD", "USDT", "USDC", "FDUSD"})

BINANCE = "binance"
COINBASE = "coinbase"
KRAKEN = "kraken"
VENUES = (BINANCE, COINBASE, KRAKEN)


@dataclass(frozen=True)
class Market:
    """A tradable pair on one venue, with the venue's own symbol spelling."""

    venue: str
    symbol: str  # venue-native: BTCUSDT / BTC-USD / BTC/USD
    base: str  # canonical asset code: BTC
    quote: str  # canonical asset code: USDT / USD
    tick_size: float | None = None
    step_size: float | None = None
    min_qty: float | None = None
    min_notional: float | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.venue, self.symbol)


@dataclass(slots=True)
class Quote:
    """Top-of-book snapshot for one market at one instant."""

    venue: str
    symbol: str
    base: str
    quote: str
    bid: float
    bid_qty: float
    ask: float
    ask_qty: float
    recv_ts: float  # epoch seconds when we received it (or fixture time in replay)
    exch_ts: float | None = None  # exchange timestamp when the venue provides one

    @property
    def key(self) -> tuple[str, str]:
        return (self.venue, self.symbol)

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread_bps(self) -> float:
        mid = self.mid
        return (self.ask - self.bid) / mid * 1e4 if mid > 0 else float("inf")

    @property
    def is_sane(self) -> bool:
        return (
            self.bid > 0
            and self.ask > 0
            and self.bid_qty >= 0
            and self.ask_qty >= 0
            and self.ask >= self.bid
        )

    def age_ms(self, now: float) -> float:
        return max(0.0, (now - self.recv_ts) * 1000.0)


@dataclass(slots=True)
class Leg:
    """One order that an opportunity requires."""

    venue: str
    symbol: str
    side: str  # "buy" or "sell" of `base`
    base: str
    quote: str
    price: float  # top-of-book price used for the estimate
    qty: float  # base quantity
    fee_rate: float  # taker fee as a fraction (0.001 == 10 bps)

    @property
    def notional(self) -> float:
        return self.price * self.qty


@dataclass
class Opportunity:
    kind: str  # "cross_exchange" | "triangular" | "anomaly"
    ts: float
    legs: list[Leg]
    gross_edge_bps: float
    net_edge_bps: float  # the DECISION edge: fee-net minus any stablecoin haircut
    notional_usd: float  # size of the first leg in USD terms
    expected_profit_usd: float  # fee-net profit at the sizes in `legs` (no haircut: it is a margin, not a cost)
    description: str
    detect_latency_ms: float = 0.0
    quote_ages_ms: list[float] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        """Stable identity for cooldowns: kind + the venue/symbol/side of each leg."""
        return self.kind + "|" + ",".join(f"{l.venue}:{l.symbol}:{l.side}" for l in self.legs)

    @property
    def executable(self) -> bool:
        return bool(self.legs) and all(l.qty > 0 for l in self.legs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "ts": self.ts,
            "gross_edge_bps": round(self.gross_edge_bps, 3),
            "net_edge_bps": round(self.net_edge_bps, 3),
            "notional_usd": round(self.notional_usd, 4),
            "expected_profit_usd": round(self.expected_profit_usd, 6),
            "detect_latency_ms": round(self.detect_latency_ms, 3),
            "quote_ages_ms": [round(a, 1) for a in self.quote_ages_ms],
            "description": self.description,
            "legs": [
                {
                    "venue": l.venue,
                    "symbol": l.symbol,
                    "side": l.side,
                    "price": l.price,
                    "qty": l.qty,
                    "fee_rate": l.fee_rate,
                }
                for l in self.legs
            ],
            "extra": self.extra,
        }


@dataclass(slots=True)
class Fill:
    venue: str
    symbol: str
    side: str
    price: float
    qty: float  # base quantity actually filled
    fee: float  # total commission in `fee_asset` (the largest component when several assets were charged)
    fee_asset: str
    ts: float
    order_id: str = ""
    fees_by_asset: dict[str, float] | None = None  # every commission asset, e.g. BNB discount running out mid-fill

    def fee_in(self, asset: str) -> float:
        if self.fees_by_asset is not None:
            return self.fees_by_asset.get(asset, 0.0)
        return self.fee if self.fee_asset == asset else 0.0

    def all_fees(self) -> dict[str, float]:
        if self.fees_by_asset is not None:
            return dict(self.fees_by_asset)
        return {self.fee_asset: self.fee} if self.fee else {}


@dataclass
class TradeRecord:
    opportunity: Opportunity
    fills: list[Fill]
    status: str  # "filled" | "partial" | "missed" | "rejected" | "pending" | "test"
    reason: str = ""
    realized_pnl_usd: float = 0.0
    ts: float = 0.0
    promised_pnl_usd: float = 0.0  # what the detector expected at the sizes that were sent
    latency_ms: float = 0.0  # detection -> last leg resolved (paper arrival model)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "status": self.status,
            "reason": self.reason,
            "realized_pnl_usd": round(self.realized_pnl_usd, 6),
            "promised_pnl_usd": round(self.promised_pnl_usd, 6),
            "latency_ms": round(self.latency_ms, 1),
            "fills": [
                {
                    "venue": f.venue,
                    "symbol": f.symbol,
                    "side": f.side,
                    "price": f.price,
                    "qty": f.qty,
                    "fee": f.fee,
                    "fee_asset": f.fee_asset,
                    "order_id": f.order_id,
                }
                for f in self.fills
            ],
            "opportunity": self.opportunity.to_dict(),
        }
