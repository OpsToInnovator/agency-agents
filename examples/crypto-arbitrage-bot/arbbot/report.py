"""Console summaries and JSONL logs. The point of this file is honesty: it
always shows how many opportunities were gross-positive next to how many
survived fees, and the best net edge seen so far."""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import ReportConfig
from .models import Opportunity, TradeRecord

log = logging.getLogger("arbbot.report")


class Reporter:
    def __init__(self, cfg: ReportConfig, engine_ref: Any, executor: Any, risk: Any, book: Any,
                 run_id: str | None = None, min_net_edge_bps: float = 0.0):
        self.cfg = cfg
        self.engine = engine_ref  # set after Engine is built (needs stats)
        self.executor = executor
        self.risk = risk
        self.book = book
        self.min_net_edge_bps = min_net_edge_bps
        self.run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self._files: dict[str, Any] = {}
        self._dir: Path | None = None
        if cfg.write_jsonl:
            self._dir = Path(cfg.log_dir) / self.run_id
            self._dir.mkdir(parents=True, exist_ok=True)
        self.rejections_seen = 0
        self._last_periodic = time.monotonic()

    # -- jsonl ------------------------------------------------------------
    def _write(self, name: str, obj: dict[str, Any]) -> None:
        if self._dir is None:
            return
        fh = self._files.get(name)
        if fh is None:
            fh = (self._dir / f"{name}.jsonl").open("a", encoding="utf-8")
            self._files[name] = fh
        fh.write(json.dumps(obj, separators=(",", ":")) + "\n")

    def close(self) -> None:
        for fh in self._files.values():
            fh.close()
        self._files.clear()

    # -- events -----------------------------------------------------------
    def on_opportunity(self, opp: Opportunity, actionable: bool) -> None:
        if actionable or self.cfg.log_every_opportunity:
            self._write("opportunities", {"actionable": actionable, **opp.to_dict()})
        if actionable:
            log.info("OPPORTUNITY %s net %+.2f bps (gross %+.2f) size $%.2f -> est. $%.4f | %s",
                     opp.kind, opp.net_edge_bps, opp.gross_edge_bps, opp.notional_usd,
                     opp.expected_profit_usd, opp.description)

    def on_anomaly(self, opp: Opportunity) -> None:
        self._write("anomalies", opp.to_dict())
        log.info("ANOMALY %s", opp.description)

    def on_rejected(self, opp: Opportunity, reason: str) -> None:
        self.rejections_seen += 1
        log.info("SKIP (%s) %s net %+.2f bps", reason, opp.kind, opp.net_edge_bps)

    def on_pending(self, record: TradeRecord) -> None:
        log.debug("SENT %s (%s) | %s", record.opportunity.kind, record.reason, record.opportunity.description)

    def on_trade(self, record: TradeRecord) -> None:
        self._write("trades", record.to_dict())
        opp = record.opportunity
        log.info("TRADE %s %s pnl %+.4f USD (promised %+.4f, %.0f ms) %s | %s", record.status.upper(), opp.kind,
                 record.realized_pnl_usd, record.promised_pnl_usd, record.latency_ms, record.reason, opp.description)

    # -- summaries --------------------------------------------------------
    def _pnl_bits(self, now: float) -> str:
        ex = self.executor
        bits = [f"trades={getattr(ex, 'trades', 0)}", f"realized={getattr(ex, 'realized_pnl_usd', 0.0):+.4f}"]
        if hasattr(ex, "latency_tax_usd"):
            bits.append(f"promised={ex.promised_pnl_usd:+.4f} missed_legs={ex.missed_legs} pending={len(ex.pending)}")
        if hasattr(ex, "equity_usd"):
            equity, unmarked = ex.equity_usd(now)
            contrib = getattr(ex, "contributions_usd", 0.0)
            bits.append(f"equity={equity:,.2f}/{contrib:,.2f}")
            if unmarked:
                bits.append(f"unmarked={len(unmarked)}")
        return " ".join(bits)

    def summary_line(self, now: float) -> str:
        st = self.engine.stats
        elapsed = max(1e-9, now - st.started)
        venues = " ".join(f"{v}={n}" for v, n in sorted(st.quotes_by_venue.items()))
        gross = " ".join(f"{k}={n}" for k, n in sorted(st.gross_by_kind.items()) if k != "anomaly") or "-"
        net = " ".join(f"{k}={n}" for k, n in sorted(st.actionable_by_kind.items())) or "0"
        p50 = st.latency_percentile(50)
        p99 = st.latency_percentile(99)
        lat = f"detect p50={p50:.2f}ms p99={p99:.2f}ms" if p50 is not None else "detect n/a"
        return (f"[{elapsed:6.0f}s] quotes={st.quotes} ({venues}) {st.quotes / elapsed:.0f}/s unchanged={st.unchanged} "
                f"markets={self.book.count()} stale={self.book.stale_count(now)} | gross>0: {gross} | "
                f"net>={self.min_net_edge_bps:g}bps: {net} | anomalies={sum(st.anomalies.values())} | "
                f"{self._pnl_bits(now)} | {lat}")

    def periodic(self, now: float) -> None:
        log.info(self.summary_line(now))

    def final(self, now: float) -> None:
        st = self.engine.stats
        lines = ["=" * 78, "FINAL SUMMARY", self.summary_line(now)]
        for kind in sorted(set(st.best_gross) | set(st.best_net)):
            if kind == "anomaly":
                continue
            g = st.best_gross.get(kind)
            n = st.best_net.get(kind)
            lines.append(f"  {kind}: best gross {g[0]:+.2f} bps | best NET {n[0]:+.2f} bps  <- {n[1]}")
        if st.anomalies:
            lines.append("  anomalies: " + ", ".join(f"{k}={v}" for k, v in sorted(st.anomalies.items())))
        if self.risk.rejections:
            lines.append("  risk rejections: " + ", ".join(f"{k}={v}" for k, v in self.risk.rejections.most_common(6)))
        if st.trades_by_status:
            lines.append("  trades: " + ", ".join(f"{k}={v}" for k, v in sorted(st.trades_by_status.items())))
        ex = self.executor
        if hasattr(ex, "latency_tax_usd") and getattr(ex, "trades", 0):
            lines.append(f"  paper fills: promised {ex.promised_pnl_usd:+.4f} USD, realized {ex.realized_pnl_usd:+.4f} USD, "
                         f"latency tax {ex.latency_tax_usd:+.4f} USD, missed legs {ex.missed_legs}")
        if st.handler_errors or st.inflight_skipped:
            lines.append(f"  handler errors {st.handler_errors}, executions skipped while one was in flight {st.inflight_skipped}")
        if st.detector_errors:
            lines.append("  detector errors: " + ", ".join(f"{k}={v}" for k, v in st.detector_errors.items()))
        if self.book.quarantined:
            lines.append("  quarantined (ticker collisions, not price errors): " + ", ".join(sorted(self.book.quarantined)))
        if st.disconnects:
            lines.append("  disconnects: " + ", ".join(f"{k}={v}" for k, v in sorted(st.disconnects.items())))
        if self._dir is not None:
            lines.append(f"  logs: {self._dir}")
        lines.append("=" * 78)
        for line in lines:
            log.info(line)
        self.close()
