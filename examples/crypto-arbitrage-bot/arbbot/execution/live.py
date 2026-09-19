"""Live execution on Binance spot. Off by default, and even when on it hits
POST /api/v3/order/test (validation only, no fill) unless real orders are
enabled in config AND on the command line.

Only single-venue (triangular) opportunities can be executed live; the
cross-exchange strategy would need order placement on Coinbase and Kraken
too, which this project deliberately does not implement.

Safety properties of the live path:
  * every order carries a newClientOrderId and its intent is appended to a
    JSONL journal and flushed BEFORE the request leaves the machine;
  * a timeout or ambiguous response is never retried blind: the order is
    looked up by client id, and if that fails trading halts (sticky) for a
    human to reconcile;
  * the kill switch, the halt flag and the current book are re-checked
    before EVERY leg, not once per opportunity;
  * timestamps use the exchange's clock (offset learned from /api/v3/time);
  * HTTP 418/429 halt trading for the process.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from ..config import LiveConfig
from ..fees import FeeSchedule
from ..filters import size_order
from ..models import BINANCE, USD_FAMILY, Fill, Leg, Opportunity, TradeRecord
from ..quotes import QuoteBook

log = logging.getLogger(__name__)


class LiveDisabled(RuntimeError):
    pass


class BinanceHTTPError(RuntimeError):
    """The venue answered, and said no. The order was NOT placed."""

    def __init__(self, status: int, payload: Any):
        self.status = status
        self.payload = payload
        code = payload.get("code") if isinstance(payload, dict) else None
        msg = payload.get("msg") if isinstance(payload, dict) else str(payload)[:200]
        super().__init__(f"binance HTTP {status} code={code}: {msg}")


class AmbiguousOrderState(RuntimeError):
    """We do not know whether the venue received the order."""


def sign_query(params: dict[str, Any], secret: str) -> str:
    """Return the urlencoded query with Binance's HMAC-SHA256 signature appended."""
    query = urlencode(params, doseq=True)
    sig = hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    return f"{query}&signature={sig}"


class BinanceRest:
    """Minimal signed REST client (aiohttp session injected for tests)."""

    def __init__(self, base_url: str, api_key: str, api_secret: str, recv_window_ms: int = 5000,
                 session: Any | None = None, public_base_url: str | None = None, timeout_s: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.public_base_url = (public_base_url or base_url).rstrip("/")
        self.api_key = api_key
        self.api_secret = api_secret
        self.recv_window_ms = recv_window_ms
        self.session = session
        self.timeout_s = timeout_s
        self.time_offset_ms = 0.0
        self.time_synced_at: float | None = None
        self.used_weight_1m: int | None = None

    async def _session(self):
        if self.session is None:
            import aiohttp

            self.session = aiohttp.ClientSession()
        return self.session

    async def close(self) -> None:
        if self.session is not None and hasattr(self.session, "close"):
            await self.session.close()

    def timestamp_ms(self) -> int:
        return int(time.time() * 1000 + self.time_offset_ms)

    async def sync_time(self) -> float:
        """Learn (server - local) in ms from the public /api/v3/time endpoint."""
        t0 = time.time()
        data = await self.request("GET", "/api/v3/time", signed=False, base=self.public_base_url)
        t1 = time.time()
        server = float(data["serverTime"])
        local_mid = (t0 + t1) / 2 * 1000
        self.time_offset_ms = server - local_mid
        self.time_synced_at = t1
        return self.time_offset_ms

    async def request(self, method: str, path: str, params: dict[str, Any] | None = None, signed: bool = True,
                      base: str | None = None) -> Any:
        params = dict(params or {})
        headers = {"User-Agent": "arbbot/0.1"}
        if signed:
            params["timestamp"] = self.timestamp_ms()
            params["recvWindow"] = self.recv_window_ms
            query = sign_query(params, self.api_secret)
            headers["X-MBX-APIKEY"] = self.api_key
        else:
            query = urlencode(params, doseq=True)
        url = f"{base or self.base_url}{path}"
        session = await self._session()
        if method == "GET":
            ctx = session.get(f"{url}?{query}" if query else url, headers=headers, timeout=self.timeout_s)
        elif method == "DELETE":
            ctx = session.delete(f"{url}?{query}", headers=headers, timeout=self.timeout_s)
        else:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            ctx = session.post(url, data=query, headers=headers, timeout=self.timeout_s)
        async with ctx as resp:
            weight = resp.headers.get("x-mbx-used-weight-1m") if getattr(resp, "headers", None) else None
            if weight is not None:
                try:
                    self.used_weight_1m = int(weight)
                except ValueError:
                    pass
            text = await resp.text() if hasattr(resp, "text") else ""
            try:
                payload = json.loads(text) if text else {}
            except ValueError:
                payload = {"raw": text[:300]}  # the .vision mirror answers HTML 404s
            if resp.status != 200:
                raise BinanceHTTPError(resp.status, payload)
            return payload


class BinanceLiveExecutor:
    remote = True  # network round trips: the engine runs it in a task, one at a time

    def __init__(self, cfg: LiveConfig, base_url: str, fees: FeeSchedule, book: QuoteBook, real_orders: bool,
                 session: Any | None = None, risk: Any | None = None, public_base_url: str | None = None,
                 intent_log: str | os.PathLike[str] | None = None, max_notional_usd: float = 100.0):
        if not cfg.enabled:
            raise LiveDisabled("live.enabled is false in config")
        api_key = os.environ.get(cfg.api_key_env, "")
        api_secret = os.environ.get(cfg.api_secret_env, "")
        if not api_key or not api_secret:
            raise LiveDisabled(f"set {cfg.api_key_env} and {cfg.api_secret_env} in the environment")
        self.cfg = cfg
        self.rest = BinanceRest(base_url, api_key, api_secret, cfg.recv_window_ms, session, public_base_url)
        self.fees = fees
        self.book = book
        self.risk = risk
        self.real_orders = bool(real_orders and cfg.real_orders)
        self.max_notional_usd = max_notional_usd
        self.intent_log = Path(intent_log) if intent_log else None
        self.trades = 0
        self.rejected = 0
        self.realized_pnl_usd = 0.0
        self.promised_pnl_usd = 0.0
        self.contributions_usd = 0.0  # unknown for live: balances live on the exchange
        log.warning("LIVE executor armed: %s", "REAL ORDERS" if self.real_orders else "test endpoint only (no fills)")

    @property
    def endpoint(self) -> str:
        return "/api/v3/order" if self.real_orders else "/api/v3/order/test"

    async def close(self) -> None:
        await self.rest.close()

    # -- preflight --------------------------------------------------------
    async def preflight(self, symbols: list[str]) -> list[str]:
        """Return a list of reasons NOT to arm live trading (empty = go)."""
        problems: list[str] = []
        try:
            offset = await self.rest.sync_time()
            if abs(offset) > self.cfg.recv_window_ms / 2:
                problems.append(f"clock skew {offset:+.0f} ms exceeds recvWindow/2 ({self.cfg.recv_window_ms / 2:.0f} ms)")
        except Exception as exc:
            problems.append(f"cannot read server time: {exc}")
        try:
            info = await self.rest.request("GET", "/api/v3/exchangeInfo", {"symbols": json.dumps(symbols, separators=(",", ":"))},
                                           signed=False, base=self.rest.public_base_url)
            status = {s["symbol"]: s.get("status") for s in info.get("symbols", [])}
            for s in symbols:
                if status.get(s) != "TRADING":
                    problems.append(f"{s}: status {status.get(s, 'unknown')}")
        except Exception as exc:
            problems.append(f"cannot read exchangeInfo: {exc}")
        try:
            restrictions = await self.rest.request("GET", "/sapi/v1/account/apiRestrictions")
            if restrictions.get("enableWithdrawals"):
                problems.append("API key can WITHDRAW: use a key without withdrawal permission")
            if not restrictions.get("enableSpotAndMarginTrading", True):
                problems.append("API key has no spot trading permission")
        except Exception as exc:
            problems.append(f"cannot read API key restrictions: {exc}")
        try:
            account = await self.rest.request("GET", "/api/v3/account", {"omitZeroBalances": "true"})
            free = {b["asset"]: float(b.get("free", 0) or 0) for b in account.get("balances", [])}
            usdt = free.get("USDT", 0.0)
            if usdt < 2 * self.max_notional_usd:
                problems.append(f"free USDT {usdt:.2f} is below 2x max notional ({2 * self.max_notional_usd:.2f})")
        except Exception as exc:
            problems.append(f"cannot read account balances: {exc}")
        try:
            await self.rest.request("POST", "/api/v3/order/test", {
                "symbol": symbols[0] if symbols else "BTCUSDT", "side": "BUY", "type": "MARKET", "quoteOrderQty": "10"})
        except Exception as exc:
            problems.append(f"order/test smoke call failed: {exc}")
        return problems

    # -- execution --------------------------------------------------------
    def _journal(self, entry: dict[str, Any]) -> None:
        if self.intent_log is None:
            return
        self.intent_log.parent.mkdir(parents=True, exist_ok=True)
        with self.intent_log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, separators=(",", ":")) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def _edge_still_there(self, leg: Leg, tolerance_bps: float = 1.0) -> tuple[bool, str]:
        q = self.book.get(BINANCE, leg.symbol)
        if q is None:
            return False, f"{leg.symbol}: no current quote"
        tol = tolerance_bps / 1e4
        if leg.side == "buy" and q.ask > leg.price * (1 + tol):
            return False, f"{leg.symbol}: ask moved {leg.price:g} -> {q.ask:g}"
        if leg.side == "sell" and q.bid < leg.price * (1 - tol):
            return False, f"{leg.symbol}: bid moved {leg.price:g} -> {q.bid:g}"
        return True, ""

    async def _send(self, params: dict[str, Any], client_id: str) -> dict[str, Any]:
        """POST the order; on an ambiguous outcome look it up by client id; never resend blind."""
        try:
            return await self.rest.request("POST", self.endpoint, params) or {}
        except BinanceHTTPError as exc:
            if exc.status in (418, 429):
                if self.risk is not None:
                    self.risk.halt(f"binance rate limit ({exc.status}); back off before re-arming", sticky=True)
            raise
        except Exception as exc:  # timeout / connection reset: did it get through?
            if not self.real_orders:
                raise AmbiguousOrderState(f"order/test call failed with {type(exc).__name__}: {exc or 'no response'}") from exc
            for attempt in range(3):
                await asyncio.sleep(0.5 * (attempt + 1))
                try:
                    found = await self.rest.request("GET", "/api/v3/order", {"symbol": params["symbol"], "origClientOrderId": client_id})
                    if found and found.get("clientOrderId") == client_id:
                        log.warning("order %s recovered by client id after ambiguous send", client_id)
                        return found
                except BinanceHTTPError as lookup_exc:
                    if isinstance(lookup_exc.payload, dict) and lookup_exc.payload.get("code") == -2013:
                        return {}  # "Order does not exist": it never arrived, nothing to reconcile
                except Exception:
                    continue
            if self.risk is not None:
                self.risk.halt(f"ambiguous order state for {client_id} ({params['symbol']}); reconcile manually", sticky=True)
            raise AmbiguousOrderState(f"ambiguous order state for {client_id} after {type(exc).__name__}: {exc or 'no response'}") from exc

    async def execute(self, opp: Opportunity, now: float) -> TradeRecord:
        if any(l.venue != BINANCE for l in opp.legs):
            self.rejected += 1
            return TradeRecord(opp, [], "rejected", "live execution supports Binance-only opportunities", 0.0, now)
        if self.rest.time_synced_at is None or time.time() - self.rest.time_synced_at > 600:
            try:
                await self.rest.sync_time()
            except Exception as exc:
                return self._abort(opp, [], now, f"cannot sync time: {exc}")
        fills: list[Fill] = []
        deltas: dict[str, float] = defaultdict(float)
        carry: float | None = None  # units of this leg's input asset delivered by the previous leg
        fee_rate = self.fees.taker(BINANCE)
        for i, leg in enumerate(opp.legs):
            if self.risk is not None:
                ok, reason = self.risk.allow_leg(time.time())
                if not ok:
                    return self._abort(opp, fills, now, f"leg {i + 1}: {reason}")
            ok, reason = self._edge_still_there(leg)
            if not ok:
                return self._abort(opp, fills, now, f"leg {i + 1}: {reason}")
            market = self.book.market(BINANCE, leg.symbol)
            if market is None:
                return self._abort(opp, fills, now, f"unknown market {leg.symbol}")
            if carry is None:
                qty = leg.qty
            elif leg.side == "sell":
                qty = min(leg.qty, carry)
            else:
                q = self.book.get(BINANCE, leg.symbol)
                qty = min(leg.qty, carry / (q.ask * (1.0 + fee_rate))) if q else leg.qty
            _, q_dec, reason = size_order(market, leg.price, qty, leg.side)
            if reason:
                return self._abort(opp, fills, now, f"{leg.symbol}: {reason}")
            client_id = f"arb{uuid.uuid4().hex[:24]}"
            params = {"symbol": leg.symbol, "side": leg.side.upper(), "type": "MARKET",
                      "quantity": format(q_dec, "f"), "newClientOrderId": client_id, "newOrderRespType": "FULL"}
            self._journal({"ts": time.time(), "kind": "intent", "endpoint": self.endpoint, "real": self.real_orders,
                           "client_id": client_id, "params": params, "opportunity": opp.description})
            try:
                payload = await self._send(params, client_id)
            except Exception as exc:
                self._journal({"ts": time.time(), "kind": "error", "client_id": client_id, "error": str(exc)})
                return self._abort(opp, fills, now, f"{leg.symbol}: {exc}")
            self._journal({"ts": time.time(), "kind": "response", "client_id": client_id,
                           "payload": payload if isinstance(payload, dict) else str(payload)[:500]})
            fill = self._fill_from(payload, leg, float(q_dec), now, client_id)
            fills.append(fill)
            if fill.side == "buy":
                deltas[leg.quote] -= fill.qty * fill.price
                deltas[leg.base] += fill.qty
                received = fill.qty - (fill.fee if fill.fee_asset == leg.base else 0.0)
                carry = received
            else:
                deltas[leg.base] -= fill.qty
                deltas[leg.quote] += fill.qty * fill.price
                received = fill.qty * fill.price - (fill.fee if fill.fee_asset == leg.quote else 0.0)
                carry = received
            if fill.fee:
                deltas[fill.fee_asset] -= fill.fee
        realized = 0.0
        for asset, d in deltas.items():
            mark = self.book.usd_rate(asset, now) if asset in USD_FAMILY else self.book.usd_price(asset, now)
            realized += d * (mark or 0.0)
        self.trades += 1
        status = "filled" if self.real_orders else "test"
        if not self.real_orders:
            realized = 0.0  # nothing was filled
        self.realized_pnl_usd += realized
        self.promised_pnl_usd += opp.expected_profit_usd
        return TradeRecord(opp, fills, status, "" if self.real_orders else "order/test: validated, not filled", realized, now,
                           promised_pnl_usd=opp.expected_profit_usd)

    def _abort(self, opp: Opportunity, fills: list[Fill], now: float, reason: str) -> TradeRecord:
        self.rejected += 1
        status = "partial" if fills and self.real_orders else "rejected"
        if status == "partial":
            log.error("LIVE: cycle aborted after %d filled leg(s): %s — inventory is now unbalanced", len(fills), reason)
            if self.risk is not None:
                self.risk.halt(f"cycle aborted mid-way ({reason}); inventory unbalanced, reconcile manually", sticky=True)
        return TradeRecord(opp, fills, status, reason, 0.0, now)

    def _fill_from(self, payload: dict[str, Any], leg: Leg, qty: float, now: float, client_id: str) -> Fill:
        if not payload or "fills" not in payload:
            # order/test returns {} : synthesize the expected fill for the record, with the
            # commission in the received asset as Binance charges it (base for buys)
            rate = self.fees.taker(BINANCE)
            if leg.side == "buy":
                return Fill(BINANCE, leg.symbol, "buy", leg.price, qty, qty * rate, leg.base, now, f"test:{client_id}")
            return Fill(BINANCE, leg.symbol, "sell", leg.price, qty, qty * leg.price * rate, leg.quote, now, f"test:{client_id}")
        executed = float(payload.get("executedQty", 0) or 0)
        quote_qty = float(payload.get("cummulativeQuoteQty", 0) or 0)
        price = quote_qty / executed if executed else leg.price
        fee = 0.0
        fee_asset = leg.quote
        for f in payload.get("fills", []):
            fee += float(f.get("commission", 0) or 0)
            fee_asset = f.get("commissionAsset", fee_asset)
        return Fill(BINANCE, leg.symbol, leg.side, price, executed, fee, fee_asset, now, str(payload.get("orderId", client_id)))
