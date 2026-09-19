"""The event loop: feeds -> quote book -> detectors -> risk -> executor -> reporter."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Any, Protocol

from .config import Config
from .detectors.base import Detector
from .execution.risk import RiskManager
from .models import Opportunity, Quote, TradeRecord
from .quotes import CHANGE_NONE, QuoteBook

log = logging.getLogger(__name__)


class Clock:
    """Wall clock in live mode; the fixture's clock in replay mode."""

    def __init__(self) -> None:
        self.override: float | None = None

    def now(self) -> float:
        return self.override if self.override is not None else time.time()

    def set(self, t: float) -> None:
        self.override = t


class FeedLike(Protocol):
    def stop(self) -> None: ...
    async def run(self, sink) -> None: ...


class ExecutorLike(Protocol):
    async def execute(self, opp: Opportunity, now: float): ...


@dataclass
class Stats:
    started: float
    quotes: int = 0
    coalesced: int = 0
    unchanged: int = 0  # quote repeated the previous top of book (no detection run)
    last_quote_ts: float | None = None
    pending: int = 0
    handler_errors: int = 0
    inflight_skipped: int = 0
    feed_errors: dict = field(default_factory=dict)  # feed name -> exception text when a feed task died
    quotes_by_venue: Counter = field(default_factory=Counter)
    gross_by_kind: Counter = field(default_factory=Counter)
    actionable_by_kind: Counter = field(default_factory=Counter)
    anomalies: Counter = field(default_factory=Counter)
    trades_by_status: Counter = field(default_factory=Counter)
    best_gross: dict[str, tuple[float, str]] = field(default_factory=dict)
    best_net: dict[str, tuple[float, str]] = field(default_factory=dict)
    latencies_ms: deque = field(default_factory=lambda: deque(maxlen=20000))
    detector_errors: Counter = field(default_factory=Counter)
    disconnects: Counter = field(default_factory=Counter)

    def note(self, opp: Opportunity, record_latency: bool = True) -> None:
        self.gross_by_kind[opp.kind] += 1
        g = self.best_gross.get(opp.kind)
        if g is None or opp.gross_edge_bps > g[0]:
            self.best_gross[opp.kind] = (opp.gross_edge_bps, opp.description)
        n = self.best_net.get(opp.kind)
        if n is None or opp.net_edge_bps > n[0]:
            self.best_net[opp.kind] = (opp.net_edge_bps, opp.description)
        if record_latency:
            self.latencies_ms.append(opp.detect_latency_ms)

    def latency_percentile(self, p: float) -> float | None:
        if not self.latencies_ms:
            return None
        data = sorted(self.latencies_ms)
        idx = min(len(data) - 1, int(round(p / 100.0 * (len(data) - 1))))
        return data[idx]


class Engine:
    def __init__(self, cfg: Config, book: QuoteBook, feeds: list[Any], detectors: list[Detector],
                 risk: RiskManager, executor: ExecutorLike, reporter: Any, clock: Clock | None = None):
        self.cfg = cfg
        self.book = book
        self.feeds = feeds
        self.detectors = detectors
        self.risk = risk
        self.executor = executor
        self.reporter = reporter
        self.clock = clock or Clock()
        self.stats = Stats(started=self.clock.now())
        self._stop = asyncio.Event()
        self._inflight: asyncio.Task | None = None  # remote (live) executions run one at a time
        self._tasks: set[asyncio.Task] = set()

    def stop(self) -> None:
        self._stop.set()

    def on_disconnect(self, venue: str) -> None:
        dropped = self.book.invalidate_venue(venue)
        self.stats.disconnects[venue] += 1
        log.warning("%s disconnected: dropped %d quotes until it reconnects", venue, dropped)

    async def run(self, duration: float | None = None) -> Stats:
        pending: dict[tuple[str, str], Quote] = {}
        event = asyncio.Event()
        stats = self.stats

        def sink(q: Quote) -> None:
            if q.key in pending:
                stats.coalesced += 1
            pending[q.key] = q
            event.set()

        feed_tasks = {asyncio.create_task(f.run(sink), name=f"feed:{getattr(f, 'venue', 'replay')}"): f for f in self.feeds}
        consumer = asyncio.create_task(self._consume(pending, event), name="consumer")
        reporter_task = asyncio.create_task(self._report_loop(), name="reporter")
        stop_task = asyncio.create_task(self._stop.wait(), name="stop")
        deadline = time.monotonic() + max(0.0, duration) if duration is not None else None
        waiting = set(feed_tasks)
        try:
            while waiting and not self._stop.is_set():
                timeout = None if deadline is None else max(0.0, deadline - time.monotonic())
                if deadline is not None and timeout <= 0:
                    break
                done, _ = await asyncio.wait(waiting | {stop_task}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
                for t in done:
                    if t is stop_task:
                        continue
                    waiting.discard(t)
                    exc = t.exception() if not t.cancelled() else None
                    if exc:
                        self.stats.feed_errors[t.get_name()] = f"{type(exc).__name__}: {exc}"
                        log.error("feed %s died: %s: %s", t.get_name(), type(exc).__name__, exc)
                    else:
                        log.info("feed %s finished", t.get_name())
        finally:
            self._stop.set()
            for f in self.feeds:
                f.stop()
            await asyncio.wait(list(feed_tasks) + [stop_task], timeout=5)
            for t in list(feed_tasks) + [stop_task]:
                if not t.done():
                    t.cancel()
            consumer.cancel()
            reporter_task.cancel()
            await asyncio.gather(consumer, reporter_task, return_exceptions=True)
            # anything the consumer had not reached yet
            for q in list(pending.values()):
                try:
                    await self.handle(q)
                except Exception:
                    self.stats.handler_errors += 1
                    log.exception("quote handler failed on %s during drain", q.key)
            pending.clear()
            if self._inflight is not None and not self._inflight.done():
                # A live order may be mid-reconciliation: give it the executor's worst case,
                # and never cancel it (a cancelled reconciliation is an unknown position).
                log.warning("waiting for the in-flight live execution to finish")
                await asyncio.wait({self._inflight}, timeout=90)
            try:
                # Summarise as of the last quote we saw: after the feeds stop every quote
                # ages past its venue's limit, which would read as "all stale, no marks".
                end = self.stats.last_quote_ts or self.clock.now()
                # orders still "in the air" in the paper arrival model settle against the last book
                for rec in self._settle(end, final=True):
                    self._finish_trade(rec, end)
                self.reporter.final(end)
            finally:
                close = getattr(self.reporter, "close", None)
                if close:
                    close()
        return self.stats

    async def _consume(self, pending: dict, event: asyncio.Event) -> None:
        while True:
            await event.wait()
            event.clear()
            batch = list(pending.values())
            pending.clear()
            for q in batch:
                try:
                    await self.handle(q)
                except asyncio.CancelledError:
                    raise
                except Exception:  # one bad quote must not stop the consumer for good
                    self.stats.handler_errors += 1
                    if self.stats.handler_errors <= 3:
                        log.exception("quote handler failed on %s", q.key)
            await asyncio.sleep(0)

    async def _report_loop(self) -> None:
        interval = max(1.0, self.cfg.report.interval_s)
        while True:
            await asyncio.sleep(interval)
            try:
                self.reporter.periodic(self.clock.now())
            except Exception:  # reporting must never take the engine down
                log.exception("reporter failed")

    def _settle(self, now: float, final: bool = False) -> list[TradeRecord]:
        settle = getattr(self.executor, "settle", None)
        if settle is None:
            return []
        return settle(now, final=final)

    async def handle(self, q: Quote) -> None:
        now = self.clock.now()
        wall = time.time()
        change = self.book.update(q)
        self.stats.quotes += 1
        self.stats.quotes_by_venue[q.venue] += 1
        self.stats.last_quote_ts = q.recv_ts
        for rec in self._settle(now):
            self._finish_trade(rec, now)
        if change == CHANGE_NONE:
            self.stats.unchanged += 1
            return
        if self._stop.is_set() and getattr(self.executor, "remote", False):
            return  # shutting down: keep the book current, start nothing that reaches a venue
        t0 = time.perf_counter()
        for det in self.detectors:
            try:
                opps = det.on_quote(q, self.book, now)
            except Exception:
                self.stats.detector_errors[det.name] += 1
                if self.stats.detector_errors[det.name] <= 3:
                    log.exception("detector %s failed on %s", det.name, q.key)
                continue
            for opp in opps:
                compute_ms = (time.perf_counter() - t0) * 1e3
                if self.clock.override is not None:
                    # Replay must be deterministic: host speed cannot decide a trade.
                    self.stats.latencies_ms.append(compute_ms)
                    opp.detect_latency_ms = 0.0
                else:
                    opp.detect_latency_ms = compute_ms + max(0.0, (wall - q.recv_ts) * 1e3)
                await self._process(opp, now)

    async def _process(self, opp: Opportunity, now: float) -> None:
        self.stats.note(opp, record_latency=self.clock.override is None)
        if opp.kind == "anomaly":
            self.stats.anomalies[opp.extra.get("subtype", "?")] += 1
            self.reporter.on_anomaly(opp)
            return
        actionable = opp.net_edge_bps >= self.cfg.detection.min_net_edge_bps
        if actionable:
            self.stats.actionable_by_kind[opp.kind] += 1
        self.reporter.on_opportunity(opp, actionable)
        if not actionable:
            return
        can = getattr(self.executor, "can_execute", None)
        if can is not None and not can(opp):
            self.reporter.on_rejected(opp, "not executable by this executor")
            return
        ok, reason = self.risk.check(opp, now)
        if not ok:
            self.reporter.on_rejected(opp, reason)
            return
        if self._stop.is_set():
            self.reporter.on_rejected(opp, "shutting down")
            return
        if getattr(self.executor, "remote", False):
            # Live orders take hundreds of ms of network time: never block the quote
            # path on them, and never have two cycles in flight at once.
            if self._inflight is not None and not self._inflight.done():
                self.stats.inflight_skipped += 1
                self.reporter.on_rejected(opp, "execution in flight")
                return
            self.risk.on_submitted(opp, now)
            task = asyncio.create_task(self._run_remote(opp, now), name="live-exec")
            self._inflight = task
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return
        self.risk.on_submitted(opp, now)
        record = await self.executor.execute(opp, now)
        record.ts = now
        if record.status == "pending":
            self.stats.pending += 1
            self.reporter.on_pending(record)
            return
        self._finish_trade(record, now)

    async def _run_remote(self, opp: Opportunity, now: float) -> None:
        try:
            record = await self.executor.execute(opp, now, min_edge_bps=self.cfg.detection.min_net_edge_bps)
        except asyncio.CancelledError:
            # Cancelled mid-order (interpreter teardown): we no longer know what the
            # venue holds. Halt sticky so nobody trades on top of an unknown position.
            self.risk.halt("shutdown while an order was in flight; reconcile manually", sticky=True)
            raise
        except Exception as exc:  # the executor halts itself on ambiguity; this is the last net
            log.exception("live execution crashed")
            record = TradeRecord(opp, [], "rejected", f"executor crashed: {exc}", 0.0, now)
        self._finish_trade(record, self.clock.now())

    def _finish_trade(self, record: TradeRecord, now: float) -> None:
        if not record.ts:
            record.ts = now
        equity = None
        equity_fn = getattr(self.executor, "equity_usd", None)
        if equity_fn is not None and record.status in ("filled", "partial"):
            value, unmarked = equity_fn(now)
            equity = value if not unmarked else None  # an unmarkable asset would fake a drawdown
        self.risk.on_settled(record, now, equity_usd=equity)
        self.stats.trades_by_status[record.status] += 1
        self.reporter.on_trade(record)
