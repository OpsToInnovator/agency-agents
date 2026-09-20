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
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Mapping
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
        if isinstance(payload, dict):
            msg = payload.get("msg")
            if msg is None:  # a non-JSON body (CDN block page, maintenance HTML) arrives as {"raw": text}
                raw = payload.get("raw")
                msg = " ".join(str(raw).split())[:200] if raw else "(empty body)"
        else:
            msg = str(payload)[:200]
        self.detail = msg
        super().__init__(f"binance HTTP {status} code={code}: {msg}")


class AmbiguousOrderState(RuntimeError):
    """We do not know whether the venue received the order."""


class OrderNeverArrived(RuntimeError):
    """The venue confirms it never saw the order (lookup answered -2013)."""


ABORT_DRIFT_BPS = 50.0  # mid-cycle, only a dislocation this large stops us completing the cycle
UNKNOWN_STATUS_CODES = (-1006, -1007)  # UNEXPECTED_RESP / TIMEOUT: "execution status unknown"
LOOKUP_ATTEMPTS = 3
UNWIND_BACKOFF_S = (0.25, 0.75)  # between unwind attempts, indexed by attempts already spent
# The only order states that are an OUTCOME. A lookup that races the matching engine can
# answer NEW or PARTIALLY_FILLED, which is a snapshot, not a result: treating it as one
# reads an order Binance is about to fill as "filled nothing".
TERMINAL_ORDER_STATUS = frozenset({"FILLED", "CANCELED", "REJECTED", "EXPIRED", "EXPIRED_IN_MATCH"})


def position_known_after(exc: BaseException | None) -> bool:
    """May we trust our own ledger after this failure: did the venue give a definitive answer
    about every order we sent? None means the leg failed before anything was sent.

    A 4xx counts as definitive ONLY when the body parsed as a Binance error object. A non-JSON
    body is rendered as {"raw": ...}, so an HTTP 400 from a proxy, CDN error page or captive
    portal arriving AFTER Binance accepted the order would otherwise read as "the leg never
    happened", and we would sell an asset we no longer hold."""
    if exc is None:
        return True
    if isinstance(exc, OrderNeverArrived):
        return True  # the venue itself confirms it never saw the order
    if isinstance(exc, BinanceHTTPError):
        if exc.status >= 500:
            return False  # 5xx / -1006 / -1007: execution status unknown
        return isinstance(exc.payload, dict) and "code" in exc.payload
    return False  # ambiguity, timeouts, cancellation, anything unnamed


def send_blocked_by(exc: BaseException | None) -> str | None:
    """Reasons no further order may leave this machine even though the position IS known.
    Sending into a rate-limit ban escalates it from minutes to days."""
    if isinstance(exc, BinanceHTTPError) and exc.status in (418, 429):
        return f"binance rate limit ({exc.status}): no further orders until the ban clears"
    return None


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
        payload = await rest.request("GET", REACHABILITY_PATH, signed=False)
    except BinanceHTTPError as exc:
        if exc.status == 451:
            return (f"{host} answered HTTP 451 (unavailable for legal reasons): Binance does not serve this "
                    f"machine's IP region. Run the bot from a permitted region on its own IP; do not tunnel through "
                    f"a VPN or proxy, which breaches the Binance terms and risks a frozen account")
        if exc.status == 403:
            return f"{host} answered HTTP 403: the request was refused (blocked IP or firewall); check the machine's IP before arming"
        return f"{host} answered HTTP {exc.status} to {REACHABILITY_PATH}: {exc.detail}"
    except Exception as exc:
        return f"cannot reach {host}: {type(exc).__name__}: {exc}"
    if payload != {}:  # GET /api/v3/ping is documented to answer exactly {}
        shown = payload["raw"] if isinstance(payload, dict) and "raw" in payload else json.dumps(payload)
        return (f"{host} answered 200 to {REACHABILITY_PATH} with a body that is not Binance's ({shown[:80]!r}): "
                f"something between this machine and Binance (proxy, web filter, captive portal) is answering; "
                f"check the machine's network path before arming")
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
        self.capital_usd = float(cfg.capital_usd or 0.0)  # the engine measures the drawdown cap against this
        self.initial_capital_usd = self.capital_usd  # the kill floor is anchored here, whatever compounding does
        self.max_cumulative_loss_pct = float(cfg.max_cumulative_loss_pct)
        self.compound = bool(cfg.compound)
        self.intent_log = Path(intent_log) if intent_log else None
        self.trades = 0
        self.rejected = 0
        self.realized_pnl_usd = 0.0
        self.promised_pnl_usd = 0.0
        self.contributions_usd = 0.0  # unknown for live: balances live on the exchange
        self.unmarked_assets: dict[str, float] = {}  # commissions in assets we cannot price (e.g. BNB off-universe)
        self.auto_unwind = bool(cfg.auto_unwind)
        self.dust_assets: dict[str, float] = {}  # residue no market will take, written off at ZERO
        self.dust_usd = 0.0
        self.unwound_cost_usd = 0.0
        self._unwinds: deque[float] = deque()  # wall clock of each successful unwind (rolling hour)
        self._t0 = time.time()  # wall clock at the start of the cycle being executed
        log.warning("LIVE executor armed: %s; auto-unwind %s",
                    "REAL ORDERS" if self.real_orders else "test endpoint only (no fills)",
                    "ON (a broken cycle sells its position back and keeps trading)" if self.auto_unwind
                    else "off (a broken cycle halts sticky)")

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
            balances = await self.read_balances()
            usdt = next((float(b.get("free", 0) or 0) for b in balances if b.get("asset") == "USDT"), 0.0)
            locked = next((float(b.get("locked", 0) or 0) for b in balances if b.get("asset") == "USDT"), 0.0)
            if usdt < 2 * self.max_notional_usd:
                problems.append(f"free USDT {usdt:.2f} is below 2x max notional ({2 * self.max_notional_usd:.2f})")
            if self.capital_usd:
                # The stake may be down by the kill budget and still re-arm (a restart after a
                # losing day, a crash, or a reconciled halt); below that floor the cumulative
                # loss rule has fired and the settings, not the balance, are what to change.
                # USDT locked in a resting order is still the stake, so it counts (as `reconcile`
                # counts it); the free-only figure above is what a cycle can actually spend.
                floor = self.kill_floor_usd
                if usdt + locked < floor - 0.01:
                    problems.append(f"USDT {usdt + locked:.2f} is below the kill floor {floor:.2f} (live.capital_usd "
                                    f"{self.initial_capital_usd:.2f} less {self.max_cumulative_loss_pct:g}% max_cumulative_loss_pct): "
                                    f"the cumulative loss budget is spent; do not restart on the same settings")
                elif usdt < self.capital_usd - 0.01:
                    log.warning("LIVE preflight: free USDT %.2f is %.2f below live.capital_usd %.2f (inside the %g%% kill "
                                "budget); the drawdown cap is measured against the declared stake",
                                usdt, self.capital_usd - usdt, self.capital_usd, self.max_cumulative_loss_pct)
            if self.auto_unwind and self.real_orders:
                foreign, unpriceable = self._foreign_balances(balances, time.time())
                if foreign:
                    problems.append(
                        f"live.auto_unwind is on and this account holds {', '.join(foreign)} besides USDT and the "
                        f"declared {self.cfg.fee_float_usd:.2f} USD BNB fee float. The unwind sells only what a cycle "
                        f"created, but it cannot tell a rejection from a sale of coins you own. Move them off this "
                        f"account, raise live.fee_float_usd, or set live.auto_unwind = false")
                if unpriceable:
                    log.warning("LIVE preflight: %s cannot be priced from the book, so auto_unwind cannot tell whether "
                                "they are yours or a cycle's; check them by hand", ", ".join(unpriceable))
        except Exception as exc:
            problems.append(f"cannot read account balances: {exc}")
        if self.compound and self.initial_capital_usd > 0:
            # live.capital_usd is the DENOMINATOR of the compounding ratios, not a multiplier of
            # the caps: a stake raised on its own shrinks the ratios it looks like it should grow.
            # Print what this file actually derives so a mis-scaled config shows up on day 0.
            per_trade = 100.0 * self.max_notional_usd / self.initial_capital_usd
            daily = (f", {100.0 * abs(self.risk.cfg.max_daily_loss_usd) / self.initial_capital_usd:.2f}% a day"
                     if self.risk is not None else "")
            log.info("LIVE preflight: compounding on; the caps re-base daily to %.2f%% of the stake per trade%s "
                     "(live.capital_usd %.2f, kill floor %.2f)",
                     per_trade, daily, self.initial_capital_usd, self.kill_floor_usd)
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

    async def read_balances(self) -> list[dict[str, Any]]:
        """Every non-zero balance on the account: one signed GET. Raises on a payload that is
        not an account, because an empty or non-JSON 200 must never read as a zero balance."""
        account = await self.rest.request("GET", "/api/v3/account", {"omitZeroBalances": "true"})
        balances = account.get("balances") if isinstance(account, dict) else None
        if not isinstance(balances, list):
            raise ValueError(f"account payload without balances: {str(account)[:120]!r}")
        return [b for b in balances if isinstance(b, dict)]

    async def read_usdt(self) -> tuple[float, float]:
        """USDT on the exchange right now as (free, locked in open orders): one signed GET.
        The compounding kill floor is measured on this number."""
        for b in await self.read_balances():
            if b.get("asset") == "USDT":
                return float(b.get("free", 0) or 0), float(b.get("locked", 0) or 0)
        return 0.0, 0.0  # omitZeroBalances: no row is a zero balance

    def _foreign_balances(self, balances: list[dict[str, Any]], now: float) -> tuple[list[str], list[str]]:
        """Everything on the account that is neither USD-family nor BNB inside the declared fee
        float, split into what we can price above dust and what we cannot price at all.

        The unwind sells only what a cycle created, never a balance read, but it cannot tell a
        venue rejection from a successful sale of coins the operator owns. An account holding
        nothing else is what turns the worst case (a mis-classified failure) from a silent,
        compounding mis-sale into one wasted order."""
        priced: list[str] = []
        unpriceable: list[str] = []
        for b in balances:
            asset = str(b.get("asset", ""))
            total = float(b.get("free", 0) or 0) + float(b.get("locked", 0) or 0)
            if not asset or asset in USD_FAMILY or total <= 0:
                continue
            mark = self.book.usd_price(asset, now)
            if mark is None:
                unpriceable.append(f"{total:.8f} {asset}")
                continue
            usd = total * mark
            allowance = self.cfg.fee_float_usd if asset == "BNB" else 0.0
            if usd > allowance + self.cfg.unwind_dust_usd:
                priced.append(f"{total:.8f} {asset} (~{usd:.2f} USD)")
        return priced, unpriceable

    @property
    def kill_floor_usd(self) -> float:
        return self.initial_capital_usd * (1.0 - self.max_cumulative_loss_pct / 100.0)

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

    def _now_plus_elapsed(self, now: float, t0: float) -> float:
        """The opportunity's clock advanced by the real time this cycle has taken. `now` is
        frozen at detection and freshness is measured as now - quote.recv_ts, so a book that
        goes dead mid-cycle never ages against it. Raw wall clock would break the fixture clock
        the offline tests and replay run on; the elapsed offset is right in production and
        stays on the opportunity's clock offline."""
        return now + max(0.0, time.time() - t0)

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
        """Longest a single execute() can take: time sync + per leg a POST and the lookups,
        plus the unwind when it can run. The engine waits this out on shutdown and never
        cancels an execution in flight: a cancelled unwind manufactures exactly the unknown
        position this feature exists to avoid."""
        t = self.rest.timeout_s
        per_order = t + LOOKUP_ATTEMPTS * (1.5 + t)
        cycle = t + 3 * per_order
        if not (self.auto_unwind and self.real_orders):
            return cycle
        return cycle + self.cfg.unwind_max_attempts * per_order + sum(UNWIND_BACKOFF_S)

    async def _reconcile(self, params: dict[str, Any], client_id: str, cause: BaseException) -> dict[str, Any]:
        """The venue may or may not have the order: look it up by client id, never resend.
        -2013 ("does not exist") only counts on the LAST attempt, because a lookup can
        race the order's arrival."""
        for attempt in range(LOOKUP_ATTEMPTS):
            await asyncio.sleep(0.5 * (attempt + 1))
            try:
                found = await self.rest.request("GET", "/api/v3/order", {"symbol": params["symbol"], "origClientOrderId": client_id})
                if found and found.get("clientOrderId") == client_id:
                    status = str(found.get("status", ""))
                    if status in TERMINAL_ORDER_STATUS:
                        log.warning("order %s recovered by client id after ambiguous send (%s)", client_id, status)
                        return found
                    # still working in the matching engine: this snapshot is not the outcome
                    log.warning("order %s found by client id but still %s; polling again",
                                client_id, status or "(no status)")
                    continue
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
        self._t0 = time.time()  # the unwind ages the book against this, not the frozen detection clock
        if not self.can_execute(opp):
            self.rejected += 1
            return TradeRecord(opp, [], "rejected", "live execution supports Binance-only opportunities", 0.0, now)
        if self.rest.time_synced_at is None or time.time() - self.rest.time_synced_at > 600:
            try:
                await self.rest.sync_time()
            except Exception as exc:
                return await self._abort(opp, [], {}, now, f"cannot sync time: {exc}", position_known=True)
        fills: list[Fill] = []
        deltas: dict[str, float] = defaultdict(float)
        carry: float | None = None  # units of this leg's input asset delivered by the previous leg
        fee_rate = self.fees.taker(BINANCE)
        for i, leg in enumerate(opp.legs):
            # Everything before the send is wrapped: after a filled leg, a failure here (a full
            # disk under the intent journal, a sizing error) must book the known fills through
            # _abort, never escape as a crash that books nothing.
            try:
                if self.risk is not None:
                    ok, reason = self.risk.allow_leg(time.time())
                    if not ok:
                        return await self._abort(opp, fills, deltas, now, f"leg {i + 1}: {reason}", position_known=True)
                gate = self._gate_leg(opp, i, min_edge_bps, now)
                if gate:
                    return await self._abort(opp, fills, deltas, now, f"leg {i + 1}: {gate}", position_known=True)
                market = self.book.market(BINANCE, leg.symbol)
                if market is None:
                    return await self._abort(opp, fills, deltas, now, f"unknown market {leg.symbol}", position_known=True)
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
                    return await self._abort(opp, fills, deltas, now, f"{leg.symbol}: {reason}", position_known=True)
                client_id = f"arb{uuid.uuid4().hex[:24]}"
                # LIMIT + IOC at the current touch: fills what is there at that price or better,
                # never chases a thin book the way a MARKET order would.
                params = {"symbol": leg.symbol, "side": leg.side.upper(), "type": "LIMIT", "timeInForce": "IOC",
                          "price": format(p_dec, "f"), "quantity": format(q_dec, "f"),
                          "newClientOrderId": client_id, "newOrderRespType": "FULL"}
                self._journal({"ts": time.time(), "kind": "intent", "endpoint": self.endpoint, "real": self.real_orders,
                               "client_id": client_id, "params": params, "opportunity": opp.description})
            except Exception as exc:  # the journal may be what failed: do not try to journal the error
                return await self._abort(opp, fills, deltas, now, f"{leg.symbol}: {exc!r} before send", position_known=True)
            try:
                payload = await self._send(params, client_id)
            except asyncio.CancelledError:
                self._journal({"ts": time.time(), "kind": "error", "client_id": client_id, "error": "cancelled mid-send: state unknown"})
                raise
            except Exception as exc:
                self._journal({"ts": time.time(), "kind": "error", "client_id": client_id, "error": str(exc)})
                return await self._abort(opp, fills, deltas, now, f"{leg.symbol}: {exc}", exc=exc)
            # The venue has answered: from here on any failure means "fill state unknown",
            # which must book what is known and halt, never read as "nothing happened".
            try:
                self._journal({"ts": time.time(), "kind": "response", "client_id": client_id,
                               "payload": payload if isinstance(payload, dict) else str(payload)[:500]})
                fill = self._fill_from(payload, leg, float(q_dec), float(p_dec), now, client_id)
                if fill is None or fill.qty <= 0:
                    status = str((payload or {}).get("status", "unknown"))
                    return await self._abort(opp, fills, deltas, now, f"{leg.symbol}: IOC order filled nothing (status {status})",
                                             position_known=True)
                fills.append(fill)
                carry = self._apply_fill(deltas, leg, fill)
            except Exception as exc:
                if self.real_orders and self.risk is not None:
                    self.risk.halt(f"error after order {client_id} was sent ({exc!r}); fill state unknown, reconcile manually", sticky=True)
                return await self._abort(opp, fills, deltas, now, f"{leg.symbol}: {exc!r} after send; fill state unknown")
        realized = self._mark_deltas(deltas, now)
        self.trades += 1
        status = "filled" if self.real_orders else "test"
        if not self.real_orders:
            realized = 0.0  # nothing was filled
        self.realized_pnl_usd += realized
        self.promised_pnl_usd += opp.expected_profit_usd
        return TradeRecord(opp, fills, status, "" if self.real_orders else "order/test: validated, not filled", realized, now,
                           promised_pnl_usd=opp.expected_profit_usd)

    def _apply_fill(self, deltas: dict[str, float], leg: Leg, fill: Fill) -> float:
        """Fold a fill into the running ledger and return the units of the RECEIVED asset it
        delivered, which is what the next leg may spend. Lifted out of execute() so a cycle leg
        and an unwind order cannot book their fees differently."""
        if fill.side == "buy":
            deltas[leg.quote] = deltas.get(leg.quote, 0.0) - fill.qty * fill.price
            deltas[leg.base] = deltas.get(leg.base, 0.0) + fill.qty
            carry = fill.qty - fill.fee_in(leg.base)
        else:
            deltas[leg.base] = deltas.get(leg.base, 0.0) - fill.qty
            deltas[leg.quote] = deltas.get(leg.quote, 0.0) + fill.qty * fill.price
            carry = fill.qty * fill.price - fill.fee_in(leg.quote)
        for asset, amount in fill.all_fees().items():
            deltas[asset] = deltas.get(asset, 0.0) - amount
        return carry

    def _start_asset(self, opp: Opportunity) -> str:
        """The asset the cycle begins and ends in. Leg 1 LEAVES the start asset: a buy spends
        the quote, a sell spends the base."""
        first = opp.legs[0]
        return first.quote if first.side == "buy" else first.base

    def _stranded(self, opp: Opportunity, deltas: Mapping[str, float], now: float,
                  skip: frozenset[str] = frozenset()) -> list[tuple[str, float, str, float | None]]:
        """Every positive non-start balance this broken cycle still holds, biggest USD mark
        first, as (asset, qty, symbol, usd). `symbol` is the cycle's OWN leg that sells `asset`
        for the start asset, or "" when no leg does: we never claim flat on an asset we cannot
        route. Negative deltas are ignored, because those are fee shorts and buying BNB back is
        not flattening, it is opening a new position. Unpriceable assets sort last but are
        still returned, so they can never be silently treated as flat."""
        start = self._start_asset(opp)
        out: list[tuple[str, float, str, float | None]] = []
        for asset, amount in deltas.items():
            if asset == start or amount <= 0 or asset in skip:
                continue
            symbol = next((l.symbol for l in opp.legs if l.base == asset and l.quote == start), "")
            mark = self.book.usd_rate(asset, now) if asset in USD_FAMILY else self.book.usd_price(asset, now)
            out.append((asset, amount, symbol, None if mark is None else mark * amount))
        out.sort(key=lambda r: (r[3] is None, -(r[3] or 0.0)))
        return out

    def _unwind_hard_stop(self, exc: BaseException) -> str | None:
        """Reasons to end an unwind at once rather than spend another attempt. Everything except
        OrderNeverArrived is a hard stop: the only outcomes worth retrying are ones that PROVE
        nothing happened, or that the order was accepted and simply did not fill."""
        if isinstance(exc, OrderNeverArrived):
            return None
        if isinstance(exc, AmbiguousOrderState):
            return (f"{exc}: the unwind order's own fill state is unknown; do NOT sell by hand until you have "
                    f"looked it up by client order id")
        if isinstance(exc, BinanceHTTPError):
            if exc.status in (418, 429):
                return f"binance rate limit ({exc.status}) during the unwind; nothing more sent"
            code = exc.payload.get("code") if isinstance(exc.payload, dict) else None
            if code == -2010:
                return (f"{exc}: the venue says we do not hold what we are selling, so our position model is wrong; "
                        f"do not trust these deltas")
            if not (isinstance(exc.payload, dict) and "code" in exc.payload):
                return f"{exc}: that reply did not come from Binance's matching engine, so this order's fill state is unknown"
            return f"{exc}"
        return f"{exc}"

    def _note_unwind(self, wall: float) -> None:
        """A successful unwind resumes trading, so the next cycle can strand again. A failed one
        halts and stops itself; this is the only bound on the succeed-then-strand-again bleed,
        which pays two taker fees plus slippage every time against an edge of a few bps."""
        self._unwinds.append(wall)
        while self._unwinds and wall - self._unwinds[0] > 3600.0:
            self._unwinds.popleft()
        if len(self._unwinds) > self.cfg.unwind_halt_after and self.risk is not None:
            self.risk.halt(f"{len(self._unwinds)} cycles broke and were unwound in the last hour (auto-unwind cost "
                           f"{self.unwound_cost_usd:+.2f} USD); the edge model or the venue is not behaving, look "
                           f"before re-arming", sticky=True)

    async def _unwind(self, opp: Opportunity, fills: list[Fill], deltas: dict[str, float], now: float,
                      t0: float) -> str:
        """Sell a broken cycle's position straight back to the start asset. Returns "" when
        nothing sellable is left (flat, or only dust), else the reason we are still exposed.
        Appends its own fills to `fills` and folds them into `deltas`, so the caller's single
        mark books the true cost of the round trip rather than a guess at a price we never
        traded at."""
        summary: dict[str, Any] = {"orders": [], "dust": {}, "attempts": 0, "flat": False, "left": ""}

        def done(left: str) -> str:
            summary["left"], summary["flat"] = left, not left
            opp.extra["unwind"] = summary
            self._journal({"ts": time.time(), "kind": "unwind_done", **summary})
            return left

        if self.risk is not None:
            ok, why = self.risk.allow_unwind(time.time())
            if not ok:
                held = self._stranded(opp, deltas, now)
                hint = (f"; holding {held[0][1]:.8f} {held[0][0]}, sell it on {held[0][2] or 'the Binance spot page'}"
                        if held else "")
                return done(f"{why}: not unwinding{hint}")
        plan = self._stranded(opp, deltas, now)
        self._journal({"ts": time.time(), "kind": "unwind_plan", "endpoint": self.endpoint, "real": self.real_orders,
                       "start_asset": self._start_asset(opp), "cycle_client_ids": [f.order_id for f in fills],
                       "stranded": {a: q for a, q, _s, _u in plan}, "route": {a: s for a, _q, s, _u in plan},
                       "opportunity": opp.description})
        anchor: dict[str, float] = {}  # asset -> the touch at the ABORT: the bound is anchored once
        dusted: set[str] = set()
        spent, last_error = 0, ""
        while True:
            targets = self._stranded(opp, deltas, now, skip=frozenset(dusted))
            if not targets:
                return done("")
            asset, qty, symbol, usd = targets[0]
            if not symbol:
                return done(f"no leg of this cycle sells {qty:.8f} {asset} into {self._start_asset(opp)} "
                            f"(its markets are {', '.join(l.symbol for l in opp.legs)})")
            market = self.book.market(BINANCE, symbol)
            if market is None:
                return done(f"{symbol}: no exchange filters to size the sale of {qty:.8f} {asset}")
            q = self._current(symbol, self._now_plus_elapsed(now, t0))
            if q is None:
                return done(f"{symbol}: no fresh sane quote to price the sale of {qty:.8f} {asset} "
                            f"(exchange halt or break, or our feed is down)")
            ref = anchor.setdefault(asset, q.bid)
            slip = self.cfg.unwind_max_slippage_bps / 1e4
            # Anchored to the touch at the abort, not re-derived each attempt: a book falling
            # 1% per 250ms would otherwise be followed all the way down in three slices while
            # every individual order honoured its bound. Here the worst case is "fills nothing".
            price = max(q.bid * (1.0 - slip), ref * (1.0 - slip))
            p_dec, q_dec, why = size_order(market, price, qty, "sell")
            if why:
                if usd is None:
                    return done(f"{symbol} rejects {qty:.8f} {asset} ({why}) and we cannot price it, so we cannot "
                                f"tell dust from inventory: not writing it off")
                if usd > self.cfg.unwind_dust_usd:
                    return done(f"{symbol} rejects {qty:.8f} {asset} (~{usd:.2f} USD) as unsellable ({why}) although "
                                f"it is worth more than the {self.cfg.unwind_dust_usd:.2f} USD dust limit: our cached "
                                f"filters disagree with the venue, or the mark is wrong")
                # Written off at ZERO. Leaving it in the ledger would have the mark value an
                # unsellable balance at its mid and book phantom gain on every broken cycle.
                deltas.pop(asset, None)
                dusted.add(asset)
                self.dust_assets[asset] = self.dust_assets.get(asset, 0.0) + qty
                self.dust_usd += usd
                summary["dust"][asset] = {"qty": qty, "usd": usd, "symbol": symbol, "filter": why}
                self._journal({"ts": time.time(), "kind": "unwind_dust", "asset": asset, "qty": qty, "usd": usd,
                               "symbol": symbol, "filter": why})
                log.warning("LIVE: %.8f %s (~%.2f USD) left as dust on %s (%s); written off at zero",
                            qty, asset, usd, symbol, why)
                if self.dust_usd > self.cfg.unwind_dust_halt_usd:
                    return done(f"{self.dust_usd:.2f} USD of unsellable dust written off this session, over the "
                                f"{self.cfg.unwind_dust_halt_usd:.2f} USD limit: sizing or filters are wrong")
                continue  # next target; no order sent, no attempt spent
            if spent >= self.cfg.unwind_max_attempts:
                return done(f"{qty:.8f} {asset} (~{usd:.2f} USD) still held on {symbol} after "
                            f"{self.cfg.unwind_max_attempts} attempt(s)" + (f"; last: {last_error}" if last_error else ""))
            if spent:
                await asyncio.sleep(UNWIND_BACKOFF_S[min(spent - 1, len(UNWIND_BACKOFF_S) - 1)])
            client_id = f"unw{uuid.uuid4().hex[:24]}"
            params = {"symbol": symbol, "side": "SELL", "type": "LIMIT", "timeInForce": "IOC",
                      "price": format(p_dec, "f"), "quantity": format(q_dec, "f"),
                      "newClientOrderId": client_id, "newOrderRespType": "FULL"}
            self._journal({"ts": time.time(), "kind": "unwind_intent", "endpoint": self.endpoint,
                           "real": self.real_orders, "client_id": client_id, "params": params,
                           "attempt": spent + 1, "of": self.cfg.unwind_max_attempts, "asset": asset,
                           "ref_touch": ref, "floor_price": ref * (1.0 - slip), "opportunity": opp.description})
            spent += 1
            summary["attempts"] = spent
            try:
                payload = await self._send(params, client_id)
            except asyncio.CancelledError:
                self._journal({"ts": time.time(), "kind": "unwind_error", "client_id": client_id,
                               "error": "cancelled mid-unwind: state unknown"})
                raise
            except Exception as send_exc:
                self._journal({"ts": time.time(), "kind": "unwind_error", "client_id": client_id, "error": str(send_exc)})
                summary["orders"].append({"client_id": client_id, "symbol": symbol, "qty": float(q_dec),
                                          "price": float(p_dec), "attempt": spent, "outcome": str(send_exc)[:200]})
                hard = self._unwind_hard_stop(send_exc)
                if hard:
                    return done(f"{symbol}: {hard}")
                last_error = f"{symbol}: {send_exc}"
                continue
            self._journal({"ts": time.time(), "kind": "unwind_response", "client_id": client_id,
                           "payload": payload if isinstance(payload, dict) else str(payload)[:500]})
            leg = Leg(BINANCE, symbol, "sell", market.base, market.quote, float(p_dec), float(q_dec),
                      self.fees.taker(BINANCE))
            fill = self._fill_from(payload, leg, float(q_dec), float(p_dec), now, client_id)
            if fill is not None and fill.qty > 0:
                fills.append(fill)
                self._apply_fill(deltas, leg, fill)
                summary["orders"].append({"client_id": client_id, "symbol": symbol, "qty": fill.qty,
                                          "price": fill.price, "attempt": spent, "outcome": "filled"})
            else:
                last_error = (f"{symbol}: IOC filled nothing at {format(p_dec, 'f')} "
                              f"(status {str((payload or {}).get('status', 'unknown'))})")
                summary["orders"].append({"client_id": client_id, "symbol": symbol, "qty": 0.0,
                                          "price": float(p_dec), "attempt": spent, "outcome": "filled nothing"})

    def _mark_deltas(self, deltas: dict[str, float], now: float, unpriced: list[str] | None = None) -> float:
        realized = 0.0
        for asset, d in deltas.items():
            if abs(d) < 1e-15:
                continue
            mark = self.book.usd_rate(asset, now) if asset in USD_FAMILY else self.book.usd_price(asset, now)
            if mark is None:
                self.unmarked_assets[asset] = self.unmarked_assets.get(asset, 0.0) + d
                if unpriced is not None:
                    unpriced.append(asset)
                log.warning("LIVE: no USD mark for %s; %.8f left out of realized PnL", asset, d)
                continue
            realized += d * mark
        return realized

    async def _abort(self, opp: Opportunity, fills: list[Fill], deltas: dict[str, float], now: float, reason: str,
                     *, exc: BaseException | None = None, position_known: bool = False) -> TradeRecord:
        """Book a broken cycle, and with live.auto_unwind on, try to get flat before halting.

        `position_known` is keyword-only and defaults to the SAFE value, so a call site added by
        a future edit is non-unwindable until someone writes position_known=True and justifies
        it in review. Where an exception exists, position_known_after(exc) decides instead."""
        self.rejected += 1
        status = "partial" if fills and self.real_orders else "rejected"
        realized, unwound = 0.0, ""
        if status == "partial":
            known = position_known_after(exc) if exc is not None else position_known
            blocked = send_blocked_by(exc)
            head = f"cycle aborted mid-way ({reason})"
            tail = "inventory unbalanced, reconcile manually"  # the runbook keys alerting off this suffix
            token = f"unw:{uuid.uuid4().hex[:12]}"
            pre_halted = self.risk is not None and self.risk.halted
            if self.risk is not None:
                if pre_halted:
                    # a rate-limit or ambiguity halt is MORE specific than ours: keep it, append,
                    # and pass no token so nothing this abort does can ever lift it
                    self.risk.halt(f"{self.risk.halt_reason}; ALSO: {head}; {tail}", sticky=True, token=None)
                else:
                    # on disk BEFORE the first unwind order leaves the machine, so a crash at any
                    # instant between "we hold something" and "we are flat" comes back halted
                    self.risk.halt(f"{head}; {tail}", sticky=True, token=token)
            stop = self.risk.allow_unwind(time.time())[1] if self.risk is not None else ""
            if not self.auto_unwind:
                unwound, left = "off", "live.auto_unwind is off"
            elif blocked:
                unwound, left = "skipped", blocked
            elif stop:
                # STOP is the one instruction that comes straight from a human and means
                # "send nothing". Name the position and the market so they can act on it.
                held = self._stranded(opp, deltas, now)
                hint = (f"; holding {held[0][1]:.8f} {held[0][0]}, sell it on {held[0][2] or 'the Binance spot page'}"
                        if held else "")
                unwound, left = "skipped", f"{stop}: not unwinding{hint}"
            elif not known:
                unwound, left = "skipped", ("fill state unknown: selling an asset we may not hold, or may hold twice, "
                                            "is worse than halting")
            else:
                left = await self._unwind(opp, fills, deltas, now, self._t0)
                unwound = "flat" if not left else "failed"
            unpriced: list[str] = []
            realized = self._mark_deltas(deltas, now, unpriced)  # AFTER the unwind: the true cost
            self.realized_pnl_usd += realized
            if not left and unpriced:  # never resume on a number we already know is wrong
                unwound, left = "failed", (f"unwound, but {', '.join(sorted(set(unpriced)))} could not be priced, so "
                                           f"the realized loss is wrong")
            if left:
                reason = f"{reason}; NOT unwound: {left}" if unwound != "flat" else f"{reason}; {left}"
                if self.risk is not None and self.risk.halt_token == token:
                    self.risk.halt(f"{head}; {left}; {tail}", sticky=True, token=token)
                elif self.risk is not None:
                    log.error("LIVE: unwind ended with %r but the halt is now %r; leaving it as it is",
                              left, self.risk.halt_reason)
            else:
                reason = f"{reason}; unwound, cycle flat"
                self.unwound_cost_usd += realized
                if self.risk is not None:
                    self.risk.resume(token)  # refuses unless the standing halt is still exactly ours
                self._note_unwind(time.time())
            log.error("LIVE: cycle aborted after %d filled leg(s): %s", len(fills), reason)
        return TradeRecord(opp, fills, status, reason, realized, now,
                           promised_pnl_usd=opp.expected_profit_usd if fills else 0.0, unwound=unwound)

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
