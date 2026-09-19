"""Live execution on Binance spot. Off by default, and even when on it hits
POST /api/v3/order/test (validation only, no fill) unless real orders are
enabled in config AND on the command line.

Only single-venue (triangular) opportunities can be executed live; the
cross-exchange strategy would need order placement on Coinbase and Kraken
too, which this project deliberately does not implement.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from collections import defaultdict
from typing import Any
from urllib.parse import urlencode

from ..config import LiveConfig
from ..fees import FeeSchedule
from ..filters import size_order
from ..models import BINANCE, USD_FAMILY, Fill, Opportunity, TradeRecord
from ..quotes import QuoteBook

log = logging.getLogger(__name__)


class LiveDisabled(RuntimeError):
    pass


def sign_query(params: dict[str, Any], secret: str) -> str:
    """Return the urlencoded query with Binance's HMAC-SHA256 signature appended."""
    query = urlencode(params, doseq=True)
    sig = hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    return f"{query}&signature={sig}"


class BinanceLiveExecutor:
    def __init__(self, cfg: LiveConfig, base_url: str, fees: FeeSchedule, book: QuoteBook,
                 real_orders: bool, session: Any | None = None):
        if not cfg.enabled:
            raise LiveDisabled("live.enabled is false in config")
        self.api_key = os.environ.get(cfg.api_key_env, "")
        self.api_secret = os.environ.get(cfg.api_secret_env, "")
        if not self.api_key or not self.api_secret:
            raise LiveDisabled(f"set {cfg.api_key_env} and {cfg.api_secret_env} in the environment")
        self.cfg = cfg
        self.base_url = base_url.rstrip("/")
        self.fees = fees
        self.book = book
        self.real_orders = bool(real_orders and cfg.real_orders)
        self.session = session
        self.trades = 0
        self.rejected = 0
        self.realized_pnl_usd = 0.0
        self.contributions_usd = 0.0  # unknown for live: balances live on the exchange
        log.warning("LIVE executor armed: %s", "REAL ORDERS" if self.real_orders else "test endpoint only (no fills)")

    @property
    def endpoint(self) -> str:
        return "/api/v3/order" if self.real_orders else "/api/v3/order/test"

    async def _session(self):
        if self.session is None:
            import aiohttp

            self.session = aiohttp.ClientSession()
        return self.session

    async def close(self) -> None:
        if self.session is not None and hasattr(self.session, "close"):
            await self.session.close()

    async def post_order(self, params: dict[str, Any]) -> dict[str, Any]:
        params = dict(params)
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = self.cfg.recv_window_ms
        body = sign_query(params, self.api_secret)
        session = await self._session()
        async with session.post(
            self.base_url + self.endpoint,
            data=body,
            headers={"X-MBX-APIKEY": self.api_key, "Content-Type": "application/x-www-form-urlencoded"},
            timeout=10,
        ) as resp:
            payload = await resp.json(content_type=None)
            if resp.status != 200:
                raise RuntimeError(f"binance {resp.status}: {payload}")
            return payload or {}

    async def execute(self, opp: Opportunity, now: float) -> TradeRecord:
        if any(l.venue != BINANCE for l in opp.legs):
            self.rejected += 1
            return TradeRecord(opp, [], "rejected", "live execution supports Binance-only opportunities", 0.0, now)
        fills: list[Fill] = []
        deltas: dict[str, float] = defaultdict(float)
        carry: float | None = None  # base quantity produced by the previous leg
        for leg in opp.legs:
            market = self.book.market(BINANCE, leg.symbol)
            if market is None:
                return self._abort(opp, fills, now, f"unknown market {leg.symbol}")
            qty = leg.qty if carry is None else min(leg.qty, carry) if leg.side == "sell" else leg.qty
            _, q, reason = size_order(market, leg.price, qty, leg.side)
            if reason:
                return self._abort(opp, fills, now, f"{leg.symbol}: {reason}")
            params = {"symbol": leg.symbol, "side": leg.side.upper(), "type": "MARKET",
                      "quantity": format(q, "f"), "newOrderRespType": "FULL"}
            try:
                payload = await self.post_order(params)
            except Exception as exc:
                return self._abort(opp, fills, now, f"{leg.symbol}: {exc}")
            fill = self._fill_from(payload, leg, float(q), now)
            fills.append(fill)
            fee_rate = self.fees.taker(BINANCE)
            if fill.side == "buy":
                deltas[leg.quote] -= fill.qty * fill.price
                deltas[leg.base] += fill.qty
                carry = fill.qty
            else:
                deltas[leg.base] -= fill.qty
                deltas[leg.quote] += fill.qty * fill.price
                carry = fill.qty * fill.price
            deltas[fill.fee_asset] -= fill.fee if fill.fee else fill.qty * fill.price * fee_rate
        realized = 0.0
        for asset, d in deltas.items():
            mark = self.book.usd_rate(asset) if asset in USD_FAMILY else self.book.usd_price(asset, now)
            realized += d * (mark or 0.0)
        self.trades += 1
        status = "filled" if self.real_orders else "test"
        if not self.real_orders:
            realized = 0.0  # nothing was filled
        self.realized_pnl_usd += realized
        return TradeRecord(opp, fills, status, "" if self.real_orders else "order/test: validated, not filled", realized, now)

    def _abort(self, opp: Opportunity, fills: list[Fill], now: float, reason: str) -> TradeRecord:
        self.rejected += 1
        status = "partial" if fills and self.real_orders else "rejected"
        if status == "partial":
            log.error("LIVE: cycle aborted after %d filled leg(s): %s — inventory is now unbalanced", len(fills), reason)
        return TradeRecord(opp, fills, status, reason, 0.0, now)

    def _fill_from(self, payload: dict[str, Any], leg, qty: float, now: float) -> Fill:
        if not payload or "fills" not in payload:
            # order/test returns {} : synthesize the expected fill for the record
            return Fill(BINANCE, leg.symbol, leg.side, leg.price, qty, qty * leg.price * self.fees.taker(BINANCE),
                        leg.quote, now, "test")
        executed = float(payload.get("executedQty", 0) or 0)
        quote_qty = float(payload.get("cummulativeQuoteQty", 0) or 0)
        price = quote_qty / executed if executed else leg.price
        fee = 0.0
        fee_asset = leg.quote
        for f in payload.get("fills", []):
            fee += float(f.get("commission", 0) or 0)
            fee_asset = f.get("commissionAsset", fee_asset)
        return Fill(BINANCE, leg.symbol, leg.side, price, executed, fee, fee_asset, now, str(payload.get("orderId", "")))
