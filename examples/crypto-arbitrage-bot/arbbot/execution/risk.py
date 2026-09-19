"""Pre-trade checks. Every opportunity passes through here before any executor,
paper or live, sees it."""
from __future__ import annotations

import os
from collections import Counter, deque
from datetime import datetime, timezone

from ..config import DetectionConfig, RiskConfig
from ..models import Opportunity, TradeRecord


class RiskManager:
    def __init__(self, cfg: RiskConfig, det: DetectionConfig):
        self.cfg = cfg
        self.det = det
        self.daily_realized_usd = 0.0
        self._day: str | None = None
        self._trade_times: deque[float] = deque()
        self._last_by_key: dict[str, float] = {}
        self.halted = False
        self.halt_reason = ""
        self.rejections: Counter[str] = Counter()

    def _roll_day(self, now: float) -> None:
        day = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")
        if day != self._day:
            self._day = day
            self.daily_realized_usd = 0.0

    def kill_switch_engaged(self) -> bool:
        return bool(self.cfg.kill_switch_file) and os.path.exists(self.cfg.kill_switch_file)

    def check(self, opp: Opportunity, now: float) -> tuple[bool, str]:
        ok, reason = self._check(opp, now)
        if not ok:
            self.rejections[reason] += 1
        return ok, reason

    def _check(self, opp: Opportunity, now: float) -> tuple[bool, str]:
        self._roll_day(now)
        if self.kill_switch_engaged():
            return False, f"kill switch file {self.cfg.kill_switch_file!r} present"
        if self.halted:
            return False, self.halt_reason
        if self.daily_realized_usd <= -abs(self.cfg.max_daily_loss_usd):
            self.halted = True
            self.halt_reason = f"daily loss cap hit ({self.daily_realized_usd:.2f} USD)"
            return False, self.halt_reason
        if not opp.executable:
            return False, "report-only"
        if opp.net_edge_bps < self.det.min_net_edge_bps:
            return False, "below min net edge"
        if opp.net_edge_bps > self.det.max_plausible_net_edge_bps:
            return False, "implausible edge (bad data?)"
        if opp.quote_ages_ms and max(opp.quote_ages_ms) > self.det.max_quote_age_ms:
            return False, "stale quote"
        if opp.detect_latency_ms > self.cfg.max_detect_latency_ms:
            return False, "slow detection"
        if opp.notional_usd > self.cfg.max_notional_per_trade_usd * 1.001:
            return False, "over max notional"
        if opp.notional_usd <= 0:
            return False, "zero notional"
        while self._trade_times and now - self._trade_times[0] > 60.0:
            self._trade_times.popleft()
        if len(self._trade_times) >= self.cfg.max_trades_per_minute:
            return False, "trade rate limit"
        last = self._last_by_key.get(opp.key)
        if last is not None and now - last < self.cfg.cooldown_s:
            return False, "cooldown"
        return True, ""

    def on_trade(self, record: TradeRecord, now: float) -> None:
        self._trade_times.append(now)
        self._last_by_key[record.opportunity.key] = now
        if record.status in ("filled", "partial"):
            self.daily_realized_usd += record.realized_pnl_usd
