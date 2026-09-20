"""Pre-trade checks. Every opportunity passes through here before any executor,
paper or live, sees it. Daily realized loss and halt state are persisted so a
restart cannot reset the cap."""
from __future__ import annotations

import json
import logging
import os
import tempfile
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path

from ..config import DetectionConfig, RiskConfig
from ..models import Opportunity, TradeRecord

log = logging.getLogger(__name__)


def utc_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


class RiskManager:
    def __init__(self, cfg: RiskConfig, det: DetectionConfig, state_file: str | os.PathLike[str] | None = None,
                 now: float | None = None):
        self.cfg = cfg
        self.det = det
        self.state_file = Path(state_file) if state_file else None
        self.daily_realized_usd = 0.0
        self._day: str | None = None
        self._trade_times: deque[float] = deque()
        self._last_by_key: dict[str, float] = {}
        self.halted = False
        self.halt_reason = ""
        self.halt_sticky = False  # sticky halts (live errors) survive the day roll; daily-cap halts do not
        # Only a halt raised WITH a token can be lifted by resume(). The auto-unwind halts
        # before it sends anything and lifts its own halt once the position is flat; a 418
        # halt, an ambiguity halt or a halt restored from disk carries no token and stands.
        self.halt_token = ""
        # Drawdown is measured on this session's own PnL curve against the capital
        # in play, so it is comparable across restarts (equity is not: paper runs
        # restart from their configured balances). The budget is per UTC day and per
        # process: the curve starts at 0 when a process starts and re-bases to the
        # day's opening PnL when the day rolls, so a restart and a day roll agree.
        # Not persisted on purpose.
        self.peak_pnl_usd: float | None = None
        self.last_pnl_usd: float | None = None
        self.rejections: Counter[str] = Counter()
        if self.state_file is not None:
            self._load_state(now)

    # -- persistence ------------------------------------------------------
    def _load_state(self, now: float | None) -> None:
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception as exc:  # corrupt file: start clean but say so
            log.warning("risk state %s unreadable (%s); starting clean", self.state_file, exc)
            return
        today = utc_day(now) if now is not None else utc_day(datetime.now(timezone.utc).timestamp())
        if data.get("day") == today:
            self.daily_realized_usd = float(data.get("daily_realized_usd", 0.0))
            self._day = today
        if data.get("halt_sticky"):
            self.halted = True
            self.halt_sticky = True
            self.halt_reason = str(data.get("halt_reason", "halted (persisted)"))
        elif data.get("halted") and data.get("day") == today:
            self.halted = True
            self.halt_reason = str(data.get("halt_reason", "halted (persisted)"))
        # A halt restored from disk belongs to a process that is gone: nothing may resume it.
        self.halt_token = ""
        if data.get("day") == today:
            # Persisted so a crash loop cannot re-fire the same cycle every few seconds and
            # blow past max_trades_per_minute while systemd restarts us.
            self._trade_times = deque(float(t) for t in data.get("trade_times", []) if isinstance(t, (int, float)))
            self._last_by_key = {str(k): float(v) for k, v in (data.get("last_by_key") or {}).items()
                                 if isinstance(v, (int, float))}
        if self.halted or self.daily_realized_usd:
            log.warning("risk state restored from %s: day=%s realized=%.4f halted=%s %s",
                        self.state_file, data.get("day"), self.daily_realized_usd, self.halted, self.halt_reason)

    def _save_state(self) -> None:
        if self.state_file is None:
            return
        payload = {"day": self._day, "daily_realized_usd": round(self.daily_realized_usd, 8), "halted": self.halted,
                   "halt_sticky": self.halt_sticky, "halt_reason": self.halt_reason, "halt_token": self.halt_token,
                   "trade_times": [round(t, 3) for t in self._trade_times],
                   "last_by_key": {k: round(v, 3) for k, v in self._last_by_key.items()}}
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".risk_state", dir=str(self.state_file.parent))
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            os.replace(tmp, self.state_file)
        except Exception as exc:  # persistence must never take trading down, but must be loud
            log.error("could not persist risk state to %s: %s", self.state_file, exc)

    # -- state ------------------------------------------------------------
    def _roll_day(self, now: float) -> None:
        day = utc_day(now)
        if day != self._day:
            self._day = day
            self.daily_realized_usd = 0.0
            if self.halted and not self.halt_sticky:
                self.halted = False
                self.halt_reason = ""
                self.halt_token = ""
            if self.peak_pnl_usd is not None:
                self.peak_pnl_usd = self.last_pnl_usd  # a fresh drawdown budget from the day's opening PnL
            self._save_state()

    def halt(self, reason: str, sticky: bool = True, token: str | None = None) -> None:
        self.halted = True
        self.halt_reason = reason
        self.halt_sticky = self.halt_sticky or sticky
        self.halt_token = token or ""  # only a halt raised WITH a token can ever be lifted by resume()
        log.error("TRADING HALTED: %s", reason)
        self._save_state()

    def resume(self, token: str) -> bool:
        """Lift ONLY the halt this process raised under exactly `token`. A rate-limit halt from
        the sender, an ambiguity halt from the order lookup, a halt that was already standing
        when we arrived, and any halt restored from disk all carry a different token or none at
        all, so they survive. Returns whether a halt was actually lifted."""
        if not token or not self.halted or self.halt_token != token:
            return False
        self.halted = False
        self.halt_sticky = False
        self.halt_reason = ""
        self.halt_token = ""
        self._save_state()
        return True

    def allow_unwind(self, now: float) -> tuple[bool, str]:
        """The gate for a risk-REDUCING order. A halt must not block it: halts stop the bot
        OPENING exposure, and refusing to flatten because we are halted for holding inventory
        is circular. The operator's explicit STOP file is the one thing that does. Deliberately
        does not roll the day: flattening is not a new trading day's business."""
        if self.kill_switch_engaged():
            return False, f"kill switch file {self.cfg.kill_switch_file!r} present"
        return True, ""

    def kill_switch_engaged(self) -> bool:
        return bool(self.cfg.kill_switch_file) and os.path.exists(self.cfg.kill_switch_file)

    def allow_leg(self, now: float) -> tuple[bool, str]:
        """Cheap gate re-checked before every live order, not once per opportunity."""
        if self.kill_switch_engaged():
            return False, f"kill switch file {self.cfg.kill_switch_file!r} present"
        self._roll_day(now)
        if self.halted:
            return False, self.halt_reason
        return True, ""

    # -- decisions --------------------------------------------------------
    def check(self, opp: Opportunity, now: float) -> tuple[bool, str]:
        ok, reason = self._check(opp, now)
        if not ok:
            self.rejections[reason] += 1
        return ok, reason

    def _check(self, opp: Opportunity, now: float) -> tuple[bool, str]:
        ok, reason = self.allow_leg(now)
        if not ok:
            return False, reason
        if self.daily_realized_usd <= -abs(self.cfg.max_daily_loss_usd):
            self.halt(f"daily loss cap hit ({self.daily_realized_usd:.2f} USD)", sticky=False)
            return False, self.halt_reason
        if not opp.executable:
            return False, "report-only"
        if opp.net_edge_bps < self.det.min_net_edge_bps:
            return False, "below min net edge"
        if opp.net_edge_bps > self.det.max_plausible_net_edge_bps:
            return False, "implausible edge (bad data?)"
        for leg, age in zip(opp.legs, opp.quote_ages_ms):
            limit = self.det.max_quote_age_ms_by_venue.get(leg.venue, self.det.max_quote_age_ms)
            if age > limit:
                return False, "stale quote"
        if opp.detect_latency_ms > self.cfg.max_detect_latency_ms:
            return False, "slow detection"
        if opp.notional_usd > self.cfg.max_notional_per_trade_usd * 1.001:
            return False, "over max notional"
        if opp.notional_usd <= 0:
            return False, "zero notional"
        if opp.expected_profit_usd < self.cfg.min_profit_usd:
            return False, "below min profit"
        while self._trade_times and now - self._trade_times[0] > 60.0:
            self._trade_times.popleft()
        if len(self._trade_times) >= self.cfg.max_trades_per_minute:
            return False, "trade rate limit"
        last = self._last_by_key.get(opp.key)
        if last is not None and now - last < self.cfg.cooldown_s:
            return False, "cooldown"
        return True, ""

    def on_submitted(self, opp: Opportunity, now: float) -> None:
        """An executor accepted the opportunity (paper plan created or live order sent)."""
        self._trade_times.append(now)
        self._last_by_key[opp.key] = now

    def on_settled(self, record: TradeRecord, now: float, pnl_usd: float | None = None,
                   capital_usd: float | None = None) -> None:
        """Fills are known: book the realized PnL against the daily cap, and the
        session PnL (realized + unrealized) against the drawdown-from-peak cap."""
        self._roll_day(now)
        if record.status in ("filled", "partial"):
            self.daily_realized_usd += record.realized_pnl_usd
            self._save_state()
        if pnl_usd is not None and capital_usd:
            self.note_pnl(pnl_usd, capital_usd)

    def note_pnl(self, pnl_usd: float, capital_usd: float) -> bool:
        """Track the peak of the session PnL curve; halt (non-sticky, lifts with the
        UTC day) when PnL has fallen max_drawdown_pct of the capital below that peak.
        The curve starts at zero, so a first loss counts against the budget."""
        self.last_pnl_usd = pnl_usd
        if self.peak_pnl_usd is None:
            self.peak_pnl_usd = 0.0
        if pnl_usd > self.peak_pnl_usd:
            self.peak_pnl_usd = pnl_usd
            return False
        pct = self.cfg.max_drawdown_pct
        if pct > 0 and capital_usd > 0 and not self.halted and (self.peak_pnl_usd - pnl_usd) >= capital_usd * pct / 100.0:
            self.halt(f"drawdown cap hit: PnL {pnl_usd:+,.2f} is {self.peak_pnl_usd - pnl_usd:,.2f} USD "
                      f"({100 * (self.peak_pnl_usd - pnl_usd) / capital_usd:.2f}% of capital) below its peak {self.peak_pnl_usd:+,.2f}",
                      sticky=False)
            return True
        return False
