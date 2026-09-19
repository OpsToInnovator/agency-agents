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
    """The venue answered with a non-200 status. A 4xx (other than -1006/-1007)
    is a definitive rejection; a 5xx or -1006/-1007 means the execution status
    is UNKNOWN and must be reconciled by client order id."""

    def __init__(self, status: int, payload: Any):
        self.status = status
        self.payload = payload
        code = payload.get("code") if isinstance(payload, dict) else None
        msg = payload.get("msg") if isinstance(payload, dict) else str(payload)[:200]
        super().__init__(f"binance HTTP {status} code={code}: {msg}")


class AmbiguousOrderState(RuntimeError):
    """We do not know whether the venue received the order."""


class OrderNeverArrived(RuntimeError):
    """The venue confirms it never saw the order (lookup answered -2013)."""


ABORT_DRIFT_BPS = 50.0  # mid-cycle, only a dislocation this large stops us completing the cycle
UNKNOWN_STATUS_CODES = (-1006, -1007)  # UNEXPECTED_RESP / TIMEOUT: "execution status unknown"
LOOKUP_ATTEMPTS = 3


REACHABILITY_PATH = "/api/v3/ping"


async def check_reachability(rest: "BinanceRest") -> str | None:
    """Ask the TRADING host (not the public market-data mirror) whether it serves this
    machine's own IP. Returns None when it answers 200, otherwise one reason not to arm.

    The public mirror answers everywhere, so without this check a geo-blocked machine
    passed the clock sync and only failed at the first signed call, with an error that
    did not say why. HTTP 451 is "unavailable for legal reasons": the answer is to run
    from a permitted region, never to tunnel around it.
    """
    host = rest.base_url
    try:
        await rest.request("GET", REACHABILITY_PATH, signed=False)
    except BinanceHTTPError as exc:
        if exc.status == 451:
            return (f"{host} answered HTTP 451 (unavailable for legal reasons): Binance does not serve this "
                    f"machine's IP region. Run the bot from a permitted region on its own IP; do not tunnel through "
                    f"a VPN or proxy, which breaches the Binance terms and risks a frozen account")
        if exc.status == 403:
            return f"{host} answered HTTP 403: the request was refused (blocked IP or firewall); check the machine's IP before arming"
        return f"{host} answered HTTP {exc.status} to {REACHABILITY_PATH}: {exc}"
    except Exception as exc:
        return f"cannot reach {host}: {type(exc).__name__}: {exc}"
    return None


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
        self.unmarked_assets: dict[str, float] = {}  # commissions in assets we cannot price (e.g. BNB off-universe)
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
        unreachable = await check_reachability(self.rest)
        if unreachable:
            # no signed call leaves the machine when the trading host will not serve it;
            # the local checks at the bottom still run so the report is complete
            problems.append(unreachable)
            return self._local_preflight(symbols, problems)
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
        return self._local_preflight(symbols, problems)

    def _local_preflight(self, symbols: list[str], problems: list[str]) -> list[str]:
        """The checks that need no network: exchange filters, kill switch, halt state."""
        missing = [s for s in symbols if (m := self.book.market(BINANCE, s)) is None or not m.step_size or not m.tick_size]
        if missing:
            problems.append(f"no exchange filters for {', '.join(missing[:6])}{'...' if len(missing) > 6 else ''} "
                            f"(use discovery, not --static)")
        if self.risk is not None and self.risk.kill_switch_engaged():
            problems.append(f"kill switch file {self.risk.cfg.kill_switch_file} already exists")
        if self.risk is not None and self.risk.halted:
            problems.append(f"trading is halted: {self.risk.halt_reason}"
                            + (" (sticky: reconcile, then remove or edit the risk state file)" if self.risk.halt_sticky else ""))
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

    @staticmethod
    def can_execute(opp: Opportunity) -> bool:
        return opp.executable and all(l.venue == BINANCE for l in opp.legs)

    def _current(self, symbol: str, now: float):
        q = self.book.get(BINANCE, symbol)
        if q is None or not q.is_sane or not self.book.is_fresh(q, now):
            return None
        return q

    def _leg_drift_bps(self, leg: Leg, now: float) -> float | None:
        """How far the touch moved against the plan, in bps (None = no fresh quote)."""
        q = self._current(leg.symbol, now)
        if q is None:
            return None
        if leg.side == "buy":
            return (q.ask / leg.price - 1.0) * 1e4
        return (1.0 - q.bid / leg.price) * 1e4

    def _cycle_edge_bps(self, legs: list[Leg], now: float) -> float | None:
        """Net edge of executing `legs` at the current book (planned sizes are irrelevant
        to the multiplier). None when any quote is missing or stale."""
        fee = self.fees.taker(BINANCE)
        mult = 1.0
        for leg in legs:
            q = self._current(leg.symbol, now)
            if q is None:
                return None
            mult *= (1.0 / q.ask) / (1.0 + fee) if leg.side == "buy" else q.bid * (1.0 - fee)
        return (mult - 1.0) * 1e4

    def _gate_leg(self, opp: Opportunity, i: int, min_edge_bps: float, now: float) -> str | None:
        """Before leg 1: the whole cycle must still clear the minimum edge. Later: we are
        holding an intermediate asset, so only a dislocation stops us completing."""
        if i == 0:
            edge = self._cycle_edge_bps(opp.legs, now)
            if edge is None:
                return "no fresh quote for every leg"
            if edge < min_edge_bps:
                return f"edge decayed to {edge:+.2f} bps before sending"
            return None
        drift = self._leg_drift_bps(opp.legs[i], now)
        if drift is None:
            return f"{opp.legs[i].symbol}: no current quote"
        if drift > ABORT_DRIFT_BPS:
            return f"{opp.legs[i].symbol}: moved {drift:.0f} bps against the plan"
        return None

    @property
    def worst_case_s(self) -> float:
        """Longest a single execute() can take: time sync + per leg a POST and the lookups."""
        t = self.rest.timeout_s
        return t + 3 * (t + LOOKUP_ATTEMPTS * (1.5 + t))

    async def _reconcile(self, params: dict[str, Any], client_id: str, cause: BaseException) -> dict[str, Any]:
        """The venue may or may not have the order: look it up by client id, never resend.
        -2013 ("does not exist") only counts on the LAST attempt, because a lookup can
        race the order's arrival."""
        for attempt in range(LOOKUP_ATTEMPTS):
            await asyncio.sleep(0.5 * (attempt + 1))
            try:
                found = await self.rest.request("GET", "/api/v3/order", {"symbol": params["symbol"], "origClientOrderId": client_id})
                if found and found.get("clientOrderId") == client_id:
                    log.warning("order %s recovered by client id after ambiguous send", client_id)
                    return found
            except BinanceHTTPError as lookup_exc:
                code = lookup_exc.payload.get("code") if isinstance(lookup_exc.payload, dict) else None
                if code == -2013 and attempt == LOOKUP_ATTEMPTS - 1:
                    raise OrderNeverArrived(f"{params['symbol']}: the venue never received order {client_id}") from cause
            except Exception:
                continue
        if self.risk is not None:
            self.risk.halt(f"ambiguous order state for {client_id} ({params['symbol']}); reconcile manually", sticky=True)
        raise AmbiguousOrderState(f"ambiguous order state for {client_id} after {type(cause).__name__}: {cause or 'no response'}") from cause

    async def _send(self, params: dict[str, Any], client_id: str) -> dict[str, Any]:
        """POST the order; on an ambiguous outcome look it up by client id; never resend blind."""
        try:
            payload = await self.rest.request("POST", self.endpoint, params)
        except BinanceHTTPError as exc:
            if exc.status in (418, 429):
                if self.risk is not None:
                    self.risk.halt(f"binance rate limit ({exc.status}); back off before re-arming", sticky=True)
                raise
            code = exc.payload.get("code") if isinstance(exc.payload, dict) else None
            if self.real_orders and (exc.status >= 500 or code in UNKNOWN_STATUS_CODES):
                return await self._reconcile(params, client_id, exc)  # "execution status unknown"
            raise
        except Exception as exc:  # timeout / connection reset: did it get through?
            if not self.real_orders:
                raise AmbiguousOrderState(f"order/test call failed with {type(exc).__name__}: {exc or 'no response'}") from exc
            return await self._reconcile(params, client_id, exc)
        if self.real_orders and not payload:
            if self.risk is not None:
                self.risk.halt(f"empty response for real order {client_id} ({params['symbol']}); reconcile manually", sticky=True)
            raise AmbiguousOrderState(f"{params['symbol']}: empty response to a real order")
        return payload or {}

    async def execute(self, opp: Opportunity, now: float, min_edge_bps: float = 0.0) -> TradeRecord:
        if not self.can_execute(opp):
            self.rejected += 1
            return TradeRecord(opp, [], "rejected", "live execution supports Binance-only opportunities", 0.0, now)
        if self.rest.time_synced_at is None or time.time() - self.rest.time_synced_at > 600:
            try:
                await self.rest.sync_time()
            except Exception as exc:
                return self._abort(opp, [], {}, now, f"cannot sync time: {exc}")
        fills: list[Fill] = []
        deltas: dict[str, float] = defaultdict(float)
        carry: float | None = None  # units of this leg's input asset delivered by the previous leg
        fee_rate = self.fees.taker(BINANCE)
        for i, leg in enumerate(opp.legs):
            if self.risk is not None:
                ok, reason = self.risk.allow_leg(time.time())
                if not ok:
                    return self._abort(opp, fills, deltas, now, f"leg {i + 1}: {reason}")
            gate = self._gate_leg(opp, i, min_edge_bps, now)
            if gate:
                return self._abort(opp, fills, deltas, now, f"leg {i + 1}: {gate}")
            market = self.book.market(BINANCE, leg.symbol)
            if market is None:
                return self._abort(opp, fills, deltas, now, f"unknown market {leg.symbol}")
            q = self._current(leg.symbol, now)
            limit_price = (q.ask if leg.side == "buy" else q.bid) if q else leg.price
            if carry is None:
                qty = leg.qty
            elif leg.side == "sell":
                qty = min(leg.qty, carry)
            else:
                # Binance takes the buy commission from the received asset, so the whole
                # carry can be spent; round_step's ROUND_DOWN is the safety margin.
                qty = min(leg.qty, carry / limit_price)
            p_dec, q_dec, reason = size_order(market, limit_price, qty, leg.side)
            if reason:
                return self._abort(opp, fills, deltas, now, f"{leg.symbol}: {reason}")
            client_id = f"arb{uuid.uuid4().hex[:24]}"
            # LIMIT + IOC at the current touch: fills what is there at that price or better,
            # never chases a thin book the way a MARKET order would.
            params = {"symbol": leg.symbol, "side": leg.side.upper(), "type": "LIMIT", "timeInForce": "IOC",
                      "price": format(p_dec, "f"), "quantity": format(q_dec, "f"),
                      "newClientOrderId": client_id, "newOrderRespType": "FULL"}
            self._journal({"ts": time.time(), "kind": "intent", "endpoint": self.endpoint, "real": self.real_orders,
                           "client_id": client_id, "params": params, "opportunity": opp.description})
            try:
                payload = await self._send(params, client_id)
            except asyncio.CancelledError:
                self._journal({"ts": time.time(), "kind": "error", "client_id": client_id, "error": "cancelled mid-send: state unknown"})
                raise
            except Exception as exc:
                self._journal({"ts": time.time(), "kind": "error", "client_id": client_id, "error": str(exc)})
                return self._abort(opp, fills, deltas, now, f"{leg.symbol}: {exc}")
            # The venue has answered: from here on any failure means "fill state unknown",
            # which must book what is known and halt, never read as "nothing happened".
            try:
                self._journal({"ts": time.time(), "kind": "response", "client_id": client_id,
                               "payload": payload if isinstance(payload, dict) else str(payload)[:500]})
                fill = self._fill_from(payload, leg, float(q_dec), float(p_dec), now, client_id)
                if fill is None or fill.qty <= 0:
                    status = str((payload or {}).get("status", "unknown"))
                    return self._abort(opp, fills, deltas, now, f"{leg.symbol}: IOC order filled nothing (status {status})")
                fills.append(fill)
                if fill.side == "buy":
                    deltas[leg.quote] -= fill.qty * fill.price
                    deltas[leg.base] += fill.qty
                    carry = fill.qty - fill.fee_in(leg.base)
                else:
                    deltas[leg.base] -= fill.qty
                    deltas[leg.quote] += fill.qty * fill.price
                    carry = fill.qty * fill.price - fill.fee_in(leg.quote)
                for asset, amount in fill.all_fees().items():
                    deltas[asset] -= amount
            except Exception as exc:
                if self.real_orders and self.risk is not None:
                    self.risk.halt(f"error after order {client_id} was sent ({exc!r}); fill state unknown, reconcile manually", sticky=True)
                return self._abort(opp, fills, deltas, now, f"{leg.symbol}: {exc!r} after send; fill state unknown")
        realized = self._mark_deltas(deltas, now)
        self.trades += 1
        status = "filled" if self.real_orders else "test"
        if not self.real_orders:
            realized = 0.0  # nothing was filled
        self.realized_pnl_usd += realized
        self.promised_pnl_usd += opp.expected_profit_usd
        return TradeRecord(opp, fills, status, "" if self.real_orders else "order/test: validated, not filled", realized, now,
                           promised_pnl_usd=opp.expected_profit_usd)

    def _mark_deltas(self, deltas: dict[str, float], now: float) -> float:
        realized = 0.0
        for asset, d in deltas.items():
            if abs(d) < 1e-15:
                continue
            mark = self.book.usd_rate(asset, now) if asset in USD_FAMILY else self.book.usd_price(asset, now)
            if mark is None:
                self.unmarked_assets[asset] = self.unmarked_assets.get(asset, 0.0) + d
                log.warning("LIVE: no USD mark for %s; %.8f left out of realized PnL", asset, d)
                continue
            realized += d * mark
        return realized

    def _abort(self, opp: Opportunity, fills: list[Fill], deltas: dict[str, float], now: float, reason: str) -> TradeRecord:
        self.rejected += 1
        status = "partial" if fills and self.real_orders else "rejected"
        realized = 0.0
        if status == "partial":
            realized = self._mark_deltas(deltas, now)  # the loss of a half-done cycle is real
            self.realized_pnl_usd += realized
            log.error("LIVE: cycle aborted after %d filled leg(s): %s — inventory is now unbalanced", len(fills), reason)
            if self.risk is not None:
                self.risk.halt(f"cycle aborted mid-way ({reason}); inventory unbalanced, reconcile manually", sticky=True)
        return TradeRecord(opp, fills, status, reason, realized, now, promised_pnl_usd=opp.expected_profit_usd if fills else 0.0)

    def _fill_from(self, payload: dict[str, Any], leg: Leg, qty: float, price: float, now: float, client_id: str) -> Fill | None:
        """Turn the venue's answer into a Fill. In test mode the answer is `{}` and the
        planned fill is synthesized (commission in the received asset, as Binance charges
        it). In real mode only what the venue reports counts: a recovered order without
        `fills` still carries executedQty/cummulativeQuoteQty, and zero executed is no fill."""
        rate = self.fees.taker(BINANCE)
        if not self.real_orders:
            if leg.side == "buy":
                return Fill(BINANCE, leg.symbol, "buy", price, qty, qty * rate, leg.base, now, f"test:{client_id}",
                            fees_by_asset={leg.base: qty * rate})
            return Fill(BINANCE, leg.symbol, "sell", price, qty, qty * price * rate, leg.quote, now, f"test:{client_id}",
                        fees_by_asset={leg.quote: qty * price * rate})
        if not payload or "executedQty" not in payload:
            return None
        executed = float(payload.get("executedQty", 0) or 0)
        if executed <= 0:
            return None
        quote_qty = float(payload.get("cummulativeQuoteQty", 0) or 0)
        avg_price = quote_qty / executed if quote_qty > 0 else price
        fees: dict[str, float] = defaultdict(float)
        if payload.get("fills"):
            for f in payload["fills"]:
                asset = str(f.get("commissionAsset", leg.quote))
                amount = float(f.get("commission", 0) or 0)
                if asset not in (leg.base, leg.quote) and self.book.usd_price(asset, now) is None and not asset in USD_FAMILY:
                    # e.g. BNB with no BNB market in the universe: count the schedule fee in the
                    # quote asset so the daily-loss cap sees it (the raw amount stays in the journal)
                    log.warning("LIVE: commission %.8f %s cannot be priced; booking the schedule fee in %s instead", amount, asset, leg.quote)
                    fees[leg.quote] += executed * avg_price * rate
                    continue
                fees[asset] += amount
        else:
            # query-order shape has no fills: assume the schedule rate in the received asset
            if leg.side == "buy":
                fees[leg.base] += executed * rate
            else:
                fees[leg.quote] += executed * avg_price * rate
        main_asset = max(fees, key=fees.get) if fees else leg.quote
        return Fill(BINANCE, leg.symbol, leg.side, avg_price, executed, fees.get(main_asset, 0.0), main_asset, now,
                    str(payload.get("orderId", client_id)), fees_by_asset=dict(fees))
