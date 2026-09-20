#!/usr/bin/env python3
"""Daily roll-up and GO / NO-GO scorecard for a multi-day paper measurement run.

    python3 scripts/rollup.py logs/measure7d [--scan-log measure7d.log] [--rtt-log rtt.log]
                              [--days 7] [--cooldown-s 2] [--capital-usd 1000]
                              [--min-day-coverage 0.95] [--include-today] [--json]

Every `scan` invocation writes <log_dir>/<run_id>/{opportunities,anomalies,trades}.jsonl. This
script pools all run directories under the given path and prints, per UTC day and per
opportunity kind: net-positive observations, opportunities that would be SENT after the
per-key cooldown, settled paper trades by status, realized and promised PnL, the latency
tax (promised - realized), fill rate, one-legged rate, dollar-weighted realized edge in
bps, PnL by asset and the share of PnL on assets the anomaly detector flagged as bad data
(price_error, venue_disagreement, crossed_book, identity_mismatch or implausible_edge; a
"jump" is a price move, not evidence of bad data) in the same UTC hour. It then scores the
GO criteria from the README on TRIANGULAR only (the only strategy the live path can
execute) over the last N complete, consecutive UTC days. A day counts as complete only when
the runs' uptime covers at least --min-day-coverage of it, so a 10-minute sample is never
scored as a day.

Nothing here is a profit forecast: it reports what the paper executor booked.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

SETTLED = ("filled", "partial", "missed")
# Subtypes that mean "bad data on this asset". "jump" is left out on purpose: a move against an
# asset's own EWMA is a price move, and excluding it would hide every volatile asset from G7.
FLAG_SUBTYPES = frozenset({"venue_disagreement", "price_error", "crossed_book", "identity_mismatch", "implausible_edge"})
TRIANGLE_QUOTES = frozenset({"BTC", "ETH", "BNB"})  # cfg.universe.triangle_quotes default
STABLES = frozenset({"USD", "USDT", "USDC", "FDUSD"})  # models.USD_FAMILY
QUOTE_SUFFIXES = ("USDT", "FDUSD", "USDC", "USD", "BTC", "ETH", "BNB", "EUR", "GBP", "AUD")
DAY_S = 86400.0
MANUAL = None  # criterion outcome: not measurable from the logs
# engine._process rewrites an over-threshold opportunity as "<kind> net <x> bps is implausible: <original>"
_IMPLAUSIBLE = re.compile(r"^(triangular|cross_exchange) net \S+ bps is implausible: ")


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------
def load_jsonl(path: Path) -> tuple[list[dict[str, Any]], int]:
    """Rows of a JSONL file; malformed lines are skipped and counted, never fatal."""
    rows: list[dict[str, Any]] = []
    bad = 0
    if not path.exists():
        return rows, bad
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                bad += 1
                continue
            if isinstance(obj, dict):
                rows.append(obj)
            else:
                bad += 1
    return rows, bad


def find_runs(root: Path) -> list[Path]:
    """`root` is either one run directory or a log_dir holding many of them."""
    names = ("opportunities.jsonl", "trades.jsonl", "anomalies.jsonl")
    if any((root / n).exists() for n in names):
        return [root]
    if not root.is_dir():
        return []
    return sorted(d for d in root.iterdir() if d.is_dir() and any((d / n).exists() for n in names))


def run_interval(run: Path, row_ts: list[float]) -> tuple[float, float] | None:
    """[start, end] of one scan process: the run id is its UTC start time (report.py), the end
    is the last row it wrote. A directory with an unparseable name starts at its first row."""
    try:
        start = datetime.strptime(run.name, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        start = min(row_ts) if row_ts else None
    if start is None:
        return None
    end = max(row_ts) if row_ts else start
    return (start, max(start, end))


def day_coverage(intervals: list[tuple[float, float]]) -> dict[str, float]:
    """Seconds of each UTC day covered by the union of the run intervals."""
    merged: list[list[float]] = []
    for s, e in sorted(intervals):
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    cov: dict[str, float] = defaultdict(float)
    for s, e in merged:
        cur = s
        while cur < e:
            day_end = cur - (cur % DAY_S) + DAY_S
            seg_end = min(e, day_end)
            cov[utc_day(cur)] += seg_end - cur
            cur = seg_end
    return dict(cov)


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def utc_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def utc_hour(ts: float) -> int:
    return int(ts // 3600)


def opp_key(opp: dict[str, Any]) -> str:
    """Same identity the risk manager uses for its per-opportunity cooldown."""
    return opp.get("kind", "?") + "|" + ",".join(
        f"{l.get('venue')}:{l.get('symbol')}:{l.get('side')}" for l in opp.get("legs", []))


def base_of_symbol(symbol: str) -> str:
    """BTCUSDT -> BTC, AR/USD -> AR, ETH-USD -> ETH, UNIBTC -> UNI."""
    for sep in ("/", "-"):
        if sep in symbol:
            return symbol.split(sep, 1)[0]
    for q in QUOTE_SUFFIXES:
        if symbol.endswith(q) and len(symbol) > len(q):
            return symbol[: -len(q)]
    return symbol


def assets_of(opp: dict[str, Any]) -> list[str]:
    """Assets an opportunity is exposed to; the first one is the attribution asset.

    cross_exchange: "AR: buy kraken AR/USD @ ..."             -> ["AR"]
    triangular:     "binance USDT -> UNI -> BTC -> USDT: ..."  -> ["UNI", "BTC"]
                    "binance USDT -> BTC -> UNI -> USDT: ..."  -> ["UNI", "BTC"]
    The detector enumerates both directions of a triangle as separate cycles, so the
    attribution asset is chosen independently of direction: the non-quote asset, or for a
    quote-only triangle (BTC/ETH) the alphabetically first member.
    """
    desc = opp.get("description", "") or ""
    kind = opp.get("kind")
    if kind == "triangular":
        head = desc.split(":", 1)[0]
        path = [p.strip() for p in head.split("->")]
        if len(path) >= 3:
            path[0] = path[0].split()[-1]  # drop the "binance " venue prefix
            start = path[0]
            middle = [a for a in path[1:-1] if a and a != start]
            if middle:
                alts = [a for a in middle if a not in TRIANGLE_QUOTES and a not in STABLES]
                pick = alts[0] if alts else min(middle)
                return [pick] + [a for a in middle if a != pick]
    m = re.match(r"([A-Z0-9]+):", desc)
    if m:
        return [m.group(1)]
    legs = opp.get("legs") or []
    if legs and legs[0].get("symbol"):
        return [base_of_symbol(str(legs[0]["symbol"]))]
    return ["?"]


def anomaly_assets(row: dict[str, Any]) -> list[str]:
    """Every asset an anomaly row exposes.

    "binance UNIBTC jump: ..."                                  -> ["UNI"]
    "ONE identity_mismatch: ..."                                -> ["ONE"]
    "triangular net +480.0 bps is implausible: binance USDT -> UNI -> BTC -> USDT: ..." -> ["UNI", "BTC"]
    (the engine rewrites the whole opportunity as an anomaly with kind="anomaly" and no legs,
    so the kind and assets come from the wrapped description)
    """
    desc = row.get("description", "") or ""
    m = _IMPLAUSIBLE.match(desc)
    if m:
        assets = assets_of({"kind": m.group(1), "description": desc[m.end():], "legs": []})
        return [a for a in assets if a != "?"]
    m = re.match(r"([A-Z0-9]+) identity_mismatch", desc)
    if m:
        return [m.group(1)]
    m = re.match(r"(?:binance|coinbase|kraken) (\S+) ", desc)
    if m:
        return [base_of_symbol(m.group(1))]
    return []


def anomaly_asset(row: dict[str, Any]) -> str | None:
    assets = anomaly_assets(row)
    return assets[0] if assets else None


def sendable(opps: list[dict[str, Any]], cooldown_s: float) -> list[dict[str, Any]]:
    """What the risk manager would let through: one send per opportunity key per cooldown."""
    out: list[dict[str, Any]] = []
    last: dict[str, float] = {}
    for opp in sorted(opps, key=lambda o: o.get("ts", 0.0)):
        k = opp_key(opp)
        ts = float(opp.get("ts", 0.0))
        if k not in last or ts - last[k] >= cooldown_s:
            out.append(opp)
            last[k] = ts
    return out


def percentile(xs: list[float], p: float) -> float:
    if not xs:
        return float("nan")
    xs = sorted(xs)
    k = (len(xs) - 1) * p
    f = int(math.floor(k))
    c = min(f + 1, len(xs) - 1)
    return xs[f] + (xs[c] - xs[f]) * (k - f)


def max_drawdown(pnls: list[float]) -> float:
    """Largest peak-to-trough fall of the cumulative PnL curve (a positive number)."""
    peak = cum = 0.0
    worst = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        worst = max(worst, peak - cum)
    return worst


# ---------------------------------------------------------------------------
# per-day roll-up
# ---------------------------------------------------------------------------
@dataclass
class DayKind:
    day: str
    kind: str
    observations: int = 0
    sendable: int = 0
    by_status: Counter = field(default_factory=Counter)
    realized: float = 0.0
    promised: float = 0.0
    notional_settled: float = 0.0
    pnl_by_asset: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    flagged_pnl: float = 0.0
    quarantined_pnl: float = 0.0
    pnls: list[float] = field(default_factory=list)  # settled trades in time order

    @property
    def settled(self) -> int:
        return sum(self.by_status[s] for s in SETTLED)

    @property
    def fill_rate(self) -> float:
        return self.by_status["filled"] / self.settled if self.settled else float("nan")

    @property
    def one_legged_rate(self) -> float:
        return self.by_status["partial"] / self.settled if self.settled else float("nan")

    @property
    def latency_tax(self) -> float:
        return self.promised - self.realized

    @property
    def realized_bps(self) -> float:
        return self.realized / self.notional_settled * 1e4 if self.notional_settled > 0 else float("nan")

    def to_dict(self) -> dict[str, Any]:
        return {
            "day": self.day, "kind": self.kind, "observations": self.observations, "sendable": self.sendable,
            "by_status": dict(self.by_status), "settled": self.settled,
            "realized_usd": round(self.realized, 6), "promised_usd": round(self.promised, 6),
            "latency_tax_usd": round(self.latency_tax, 6), "fill_rate": self.fill_rate,
            "one_legged_rate": self.one_legged_rate, "realized_bps": self.realized_bps,
            "notional_settled_usd": round(self.notional_settled, 4),
            "pnl_by_asset": {k: round(v, 6) for k, v in sorted(self.pnl_by_asset.items(), key=lambda kv: -kv[1])},
            "flagged_pnl_usd": round(self.flagged_pnl, 6), "quarantined_pnl_usd": round(self.quarantined_pnl, 6),
            "max_drawdown_usd": round(max_drawdown(self.pnls), 6),
        }


def rollup(opps: list[dict[str, Any]], trades: list[dict[str, Any]], anomalies: list[dict[str, Any]],
           cooldown_s: float = 2.0) -> dict[tuple[str, str], DayKind]:
    out: dict[tuple[str, str], DayKind] = {}

    def slot(day: str, kind: str) -> DayKind:
        return out.setdefault((day, kind), DayKind(day, kind))

    actionable = [o for o in opps if o.get("actionable", True) and o.get("kind") != "anomaly"]
    for o in actionable:
        slot(utc_day(float(o["ts"])), o.get("kind", "?")).observations += 1
    for o in sendable(actionable, cooldown_s):
        slot(utc_day(float(o["ts"])), o.get("kind", "?")).sendable += 1

    flagged_hours: dict[str, set[int]] = defaultdict(set)  # asset -> UTC hours it was flagged in
    quarantined: set[str] = set()
    for a in anomalies:
        subtype = (a.get("extra") or {}).get("subtype")
        if subtype not in FLAG_SUBTYPES:
            continue
        hour = utc_hour(float(a.get("ts", 0.0)))
        for asset in anomaly_assets(a):
            flagged_hours[asset].add(hour)
            if subtype == "identity_mismatch":
                quarantined.add(asset)

    for t in sorted(trades, key=lambda r: r.get("ts", 0.0)):
        opp = t.get("opportunity") or {}
        kind = opp.get("kind", "?")
        ts = float(t.get("ts") or opp.get("ts") or 0.0)
        dk = slot(utc_day(ts), kind)
        status = t.get("status", "?")
        dk.by_status[status] += 1
        if status not in SETTLED:
            continue
        realized = float(t.get("realized_pnl_usd", 0.0))
        dk.realized += realized
        dk.promised += float(t.get("promised_pnl_usd", 0.0))
        dk.notional_settled += float(opp.get("notional_usd", 0.0))
        dk.pnls.append(realized)
        assets = assets_of(opp)
        dk.pnl_by_asset[assets[0]] += realized
        hour = utc_hour(ts)
        if any(hour in flagged_hours.get(a, ()) for a in assets):
            dk.flagged_pnl += realized
        if any(a in quarantined for a in assets):
            dk.quarantined_pnl += realized
    return out


# ---------------------------------------------------------------------------
# scan-log and rtt-log parsing (optional inputs)
# ---------------------------------------------------------------------------
_START = re.compile(r"arbbot \S+ starting in .*? mode")
_SUMMARY = re.compile(r"\[\s*(\d+)s\] quotes=.*?markets=(\d+) stale=(\d+) \| gross>0: (.*?) \|")
_DISCONNECTS = re.compile(r"disconnects: (.*)$")
_HANDLER = re.compile(r"handler errors (\d+)")
_HALT = re.compile(r"TRADING HALTED: (.*)$")


def parse_scan_log(path: Path) -> dict[str, Any]:
    """Counters from the scan's stderr. Each process logs one "arbbot ... starting in ... mode"
    line, which starts a new segment; cumulative counters are taken at their maximum within a
    segment and summed across segments. Logs without start lines (rotated or truncated) fall
    back to "elapsed went backwards" as the segment boundary."""
    gross: Counter = Counter()
    seg_gross: Counter = Counter()
    last_elapsed = -1
    saw_start = False
    stale_shares: list[float] = []
    disconnects: Counter = Counter()
    handler_errors = 0
    halts: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if _START.search(line):
            gross.update(seg_gross)
            seg_gross = Counter()
            last_elapsed = -1
            saw_start = True
            continue
        m = _SUMMARY.search(line)
        if m:
            elapsed = int(m.group(1))
            if not saw_start and elapsed < last_elapsed:
                gross.update(seg_gross)
                seg_gross = Counter()
            last_elapsed = elapsed
            markets, stale = int(m.group(2)), int(m.group(3))
            if markets:
                stale_shares.append(stale / markets)
            for part in m.group(4).split():
                if "=" in part:
                    k, v = part.split("=", 1)
                    if v.isdigit():
                        seg_gross[k] = max(seg_gross[k], int(v))  # cumulative within a run
            continue
        m = _DISCONNECTS.search(line)
        if m:
            for part in m.group(1).split(","):
                part = part.strip()
                if "=" in part:
                    k, v = part.split("=", 1)
                    if v.strip().isdigit():
                        disconnects[k.strip()] += int(v)
            continue
        m = _HANDLER.search(line)
        if m:
            handler_errors += int(m.group(1))
            continue
        m = _HALT.search(line)
        if m:
            halts.append(m.group(1).strip())
    gross.update(seg_gross)
    return {
        "gross_by_kind": dict(gross),
        "stale_share_mean": statistics.fmean(stale_shares) if stale_shares else float("nan"),
        "disconnects": dict(disconnects),
        "handler_errors": handler_errors,
        "halts": halts,
        "daily_loss_halts": sum(1 for h in halts if "daily" in h.lower()),
    }


def parse_rtt_log(path: Path) -> dict[str, dict[str, Any]]:
    """rtt_probe.sh lines: `<ts> <venue> warm_ms <n> total_ms <n> http <code>`.

    Only a 2xx answer is a round trip to the API. A numeric time with a 451/403 code is the
    edge refusing the request (counted as `blocked`); nan or 000 is a curl failure (`failed`).
    Every venue seen is returned, so a fully blocked venue shows up with n=0.
    """
    warm: dict[str, list[float]] = defaultdict(list)
    blocked: Counter = Counter()
    failed: Counter = Counter()
    seen: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[2] != "warm_ms":
            continue
        venue = parts[1]
        if venue not in seen:
            seen.append(venue)
        code = parts[7] if len(parts) >= 8 and parts[6] == "http" else "200"  # older probe lines had no code
        try:
            value = float(parts[3])
        except ValueError:
            failed[venue] += 1
            continue
        if math.isnan(value) or code == "000":
            failed[venue] += 1
        elif not code.startswith("2"):
            blocked[venue] += 1
        else:
            warm[venue].append(value)
    return {v: {"n": len(warm[v]), "p50_ms": percentile(warm[v], 0.5), "p90_ms": percentile(warm[v], 0.9),
                "blocked": blocked[v], "failed": failed[v]} for v in seen}


# ---------------------------------------------------------------------------
# GO / NO-GO scorecard (triangular only)
# ---------------------------------------------------------------------------
@dataclass
class Criterion:
    code: str
    text: str
    passed: bool | None  # None = could not be measured from the inputs given
    detail: str

    @property
    def label(self) -> str:
        return {True: "PASS", False: "FAIL", None: "NOT MEASURED"}[self.passed]


G7_TEXT = ("no asset > 40% of PnL, <= 20% from assets flagged price_error / venue_disagreement / crossed_book / "
           "identity_mismatch / implausible_edge in the same hour, 0 from quarantined tickers")


def scorecard(days: list[DayKind], capital_usd: float, n_days: int, ops: dict[str, Any] | None,
              min_daily_pnl_usd: float = 2.0, min_trades_per_day: int = 20) -> tuple[list[Criterion], str]:
    """Score the README's GO criteria on per-day TRIANGULAR stats (already limited to the window)."""
    crit: list[Criterion] = []
    days = sorted(days, key=lambda d: d.day)
    settled = sum(d.settled for d in days)
    daily = [d.realized for d in days]
    total = sum(daily)
    promised = sum(d.promised for d in days)
    notional = sum(d.notional_settled for d in days)
    by_status: Counter = Counter()
    for d in days:
        by_status.update(d.by_status)
    filled = by_status["filled"]
    partial = by_status["partial"]

    need = min_trades_per_day * n_days
    crit.append(Criterion("G1", f">= {need} settled paper trades ({min_trades_per_day}/day)", settled >= need,
                          f"{settled} settled over {len(days)} day(s)"))
    allowed_negative_days = max(0, n_days - math.ceil(n_days * 5 / 7))
    mean_daily = total / n_days if n_days else float("nan")
    neg_days = sum(1 for p in daily if p < 0) + max(0, n_days - len(days))
    crit.append(Criterion("G2", f"realized >= +US${min_daily_pnl_usd:.2f}/day mean and >= 0 on all but {allowed_negative_days} day(s)",
                          total > 0 and mean_daily >= min_daily_pnl_usd and neg_days <= allowed_negative_days,
                          f"total {total:+.4f} USD, mean {mean_daily:+.4f}/day, {neg_days} negative or missing day(s)"))
    bps = total / notional * 1e4 if notional > 0 else float("nan")
    crit.append(Criterion("G3", "dollar-weighted realized edge >= +1.0 bps per settled trade", notional > 0 and bps >= 1.0,
                          f"{bps:+.3f} bps on {notional:,.2f} USD settled notional" if notional > 0 else "no settled notional"))
    fill_rate = filled / settled if settled else float("nan")
    one_legged = partial / settled if settled else float("nan")
    crit.append(Criterion("G4", "fill rate >= 60% and one-legged (partial) rate <= 10% of sends",
                          settled > 0 and fill_rate >= 0.6 and one_legged <= 0.10,
                          f"fill {fill_rate:.1%}, one-legged {one_legged:.1%} of {settled} sends" if settled else "no sends"))
    ratio = total / promised if promised > 0 else float("nan")
    crit.append(Criterion("G5", "realized / promised >= 0.5 (latency tax eats less than half)", promised > 0 and ratio >= 0.5,
                          f"realized {total:+.4f} / promised {promised:+.4f} = {ratio:.2f}" if promised > 0 else "nothing promised"))
    padded = daily + [0.0] * max(0, n_days - len(daily))
    if len(padded) >= 2 and statistics.pstdev(padded) > 0:
        t_stat = statistics.fmean(padded) / (statistics.stdev(padded) / math.sqrt(len(padded)))
    else:
        t_stat = float("nan")
    crit.append(Criterion("G6", "t-stat of daily PnL (mean / (sd / sqrt(days))) >= 2.0", not math.isnan(t_stat) and t_stat >= 2.0,
                          f"t = {t_stat:.2f} over {len(padded)} day(s)"))
    by_asset: Counter = Counter()
    for d in days:
        for a, v in d.pnl_by_asset.items():
            by_asset[a] += v
    top_asset, top_pnl = (by_asset.most_common(1)[0] if by_asset else ("-", 0.0))
    top_share = top_pnl / total if total > 0 else float("nan")
    flagged = sum(d.flagged_pnl for d in days)
    flagged_share = flagged / total if total > 0 else float("nan")
    quarantined = sum(d.quarantined_pnl for d in days)
    crit.append(Criterion("G7", G7_TEXT,
                          total > 0 and top_share <= 0.40 and flagged_share <= 0.20 and abs(quarantined) < 1e-9,
                          f"top asset {top_asset} {top_share:.0%}, flagged {flagged_share:.0%}, quarantined {quarantined:+.4f} USD"
                          if total > 0 else "no positive PnL to attribute"))
    dd = max_drawdown([p for d in days for p in d.pnls])
    dd_ok = dd <= 0.02 * capital_usd
    if ops is None:
        crit.append(Criterion("G8", "max drawdown <= 2% of the capital in play and zero daily-loss-cap halts",
                              MANUAL if dd_ok else False,
                              f"drawdown {dd:.4f} USD vs {0.02 * capital_usd:.2f} allowed; halts need --scan-log"))
    else:
        halts = int(ops.get("daily_loss_halts", 0))
        crit.append(Criterion("G8", "max drawdown <= 2% of the capital in play and zero daily-loss-cap halts",
                              dd_ok and halts == 0, f"drawdown {dd:.4f} USD vs {0.02 * capital_usd:.2f} allowed; {halts} daily-loss halt(s)"))
    if ops is None:
        crit.append(Criterion("G9", "ops: < 5 disconnects/day/venue, 0 handler errors, stale share < 30%", MANUAL, "needs --scan-log"))
    else:
        worst_venue = max(ops["disconnects"].values(), default=0) / max(1, n_days)
        stale = ops["stale_share_mean"]
        ok = worst_venue < 5 and ops["handler_errors"] == 0 and (not math.isnan(stale) and stale < 0.30)
        crit.append(Criterion("G9", "ops: < 5 disconnects/day/venue, 0 handler errors, stale share < 30%", ok,
                              f"worst venue {worst_venue:.1f} disconnects/day, {ops['handler_errors']} handler errors, "
                              f"stale share {stale:.0%}" if not math.isnan(stale) else "no summary lines found in the scan log"))
    crit.append(Criterion("G10", "replaying the run's recorded tapes at fees +25% and slippage 5 bps still shows positive realized PnL",
                          MANUAL, "run: python3 scripts/sweep.py <tape> --config measure7d.toml --fee-scale 1.25 --slippage 5"))
    if any(c.passed is False for c in crit):
        verdict = "NO-GO"
    elif any(c.passed is None and c.code != "G10" for c in crit):
        verdict = "INCOMPLETE (pass --scan-log to score G8/G9)"
    else:
        verdict = "GO, pending the manual G10 replay check"
    return crit, verdict


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _consecutive(days: list[str]) -> bool:
    ords = [date.fromisoformat(d).toordinal() for d in days]
    return all(b - a == 1 for a, b in zip(ords, ords[1:]))


def build_report(root: Path, days: int, cooldown_s: float, capital_usd: float, include_today: bool,
                 scan_log: Path | None, rtt_log: Path | None, today: str | None = None,
                 min_day_coverage: float = 0.95) -> dict[str, Any]:
    runs = find_runs(root)
    opps: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    anomalies: list[dict[str, Any]] = []
    intervals: list[tuple[float, float]] = []
    malformed = 0
    for run in runs:
        row_ts: list[float] = []
        for name, sink in (("opportunities.jsonl", opps), ("trades.jsonl", trades), ("anomalies.jsonl", anomalies)):
            rows, bad = load_jsonl(run / name)
            sink.extend(rows)
            malformed += bad
            row_ts.extend(float(r["ts"]) for r in rows if isinstance(r.get("ts"), (int, float)))
        iv = run_interval(run, row_ts)
        if iv is not None:
            intervals.append(iv)
    table = rollup(opps, trades, anomalies, cooldown_s)
    ops = parse_scan_log(scan_log) if scan_log else None
    rtt = parse_rtt_log(rtt_log) if rtt_log else None
    today = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    coverage = day_coverage(intervals)
    all_days = sorted({d for d, _ in table} | set(coverage))
    complete: list[str] = []
    partial: dict[str, float] = {}
    for d in all_days:
        if d > today:
            continue
        if d == today:
            if include_today:
                complete.append(d)
            continue
        if coverage.get(d, 0.0) >= min_day_coverage * DAY_S:
            complete.append(d)
        else:
            partial[d] = round(coverage.get(d, 0.0) / 3600.0, 2)
    window = complete[-days:]
    tri = [dk for (d, k), dk in table.items() if k == "triangular" and d in window]
    crit, verdict = scorecard(tri, capital_usd, days, ops)
    if len(window) < days:
        verdict = f"INCOMPLETE: {len(window)} of {days} complete UTC day(s) so far"
        if partial:
            verdict += " (partial days excluded: " + ", ".join(f"{d} {h:g} h" for d, h in partial.items()) + ")"
    elif not _consecutive(window):
        verdict = "INCOMPLETE: the last " + str(days) + " complete days are not consecutive (" + ", ".join(window) + ")"
    return {
        "runs": [str(r) for r in runs], "malformed_rows": malformed,
        "days_seen": all_days, "coverage_hours": {d: round(s / 3600.0, 2) for d, s in sorted(coverage.items())},
        "partial_days": partial, "window": window, "days_required": days,
        "daily": [table[k].to_dict() for k in sorted(table)],
        "ops": ops, "rtt": rtt,
        "scorecard": [{"code": c.code, "criterion": c.text, "result": c.label, "detail": c.detail} for c in crit],
        "verdict": verdict,
    }


def _fmt(x: float, spec: str = ".4f") -> str:
    return "n/a" if isinstance(x, float) and math.isnan(x) else format(x, spec)


def _jsonable(x: Any) -> Any:
    """NaN and inf are not JSON; emit null so --json output parses everywhere."""
    if isinstance(x, float) and not math.isfinite(x):
        return None
    if isinstance(x, dict):
        return {k: _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    return x


def print_report(rep: dict[str, Any]) -> None:
    print(f"runs: {len(rep['runs'])}   malformed rows skipped: {rep['malformed_rows']}   "
          f"days seen: {', '.join(rep['days_seen']) or '-'}")
    print(f"scoring window ({rep['days_required']} complete UTC days): {', '.join(rep['window']) or '-'}")
    if rep["partial_days"]:
        print("excluded as partial (uptime hours): " + ", ".join(f"{d} {h:g} h" for d, h in rep["partial_days"].items()))
    print()
    hdr = f"{'day':10} {'kind':14} {'obs':>7} {'sendable':>8} {'settled':>7} {'filled':>6} {'partial':>7} {'missed':>6} " \
          f"{'realized':>10} {'promised':>10} {'lat.tax':>9} {'fill%':>6} {'1-leg%':>6} {'bps':>7} {'flagged$':>9}"
    print(hdr)
    for row in rep["daily"]:
        s = row["by_status"]
        print(f"{row['day']:10} {row['kind']:14} {row['observations']:7d} {row['sendable']:8d} {row['settled']:7d} "
              f"{s.get('filled', 0):6d} {s.get('partial', 0):7d} {s.get('missed', 0):6d} "
              f"{row['realized_usd']:+10.4f} {row['promised_usd']:+10.4f} {row['latency_tax_usd']:+9.4f} "
              f"{_fmt(row['fill_rate'] * 100, '.0f') if not math.isnan(row['fill_rate']) else 'n/a':>6} "
              f"{_fmt(row['one_legged_rate'] * 100, '.0f') if not math.isnan(row['one_legged_rate']) else 'n/a':>6} "
              f"{_fmt(row['realized_bps'], '+.2f'):>7} {row['flagged_pnl_usd']:+9.4f}")
        top = list(row["pnl_by_asset"].items())[:5]
        if top:
            print(f"{'':25} by asset: " + ", ".join(f"{a} {v:+.4f}" for a, v in top))
    if rep["ops"]:
        o = rep["ops"]
        print()
        print(f"scan log: gross-positive {o['gross_by_kind']}, stale share {_fmt(o['stale_share_mean'], '.1%')}, "
              f"disconnects {o['disconnects']}, handler errors {o['handler_errors']}, halts {len(o['halts'])} "
              f"({o['daily_loss_halts']} daily-loss)")
    if rep["rtt"]:
        print("rtt (warm, ms): " + ", ".join(
            f"{v} p50={_fmt(r['p50_ms'], '.0f')} p90={_fmt(r['p90_ms'], '.0f')} (n={r['n']}, blocked={r['blocked']}, failed={r['failed']})"
            for v, r in sorted(rep["rtt"].items())))
        for v, r in sorted(rep["rtt"].items()):
            if r["blocked"]:
                print(f"WARNING: {v} answered non-2xx on {r['blocked']} probe(s); this machine may be geo-blocked "
                      f"and its p90 is not a usable assumed_rtt_ms", file=sys.stderr)
    print()
    print("GO / NO-GO scorecard (triangular only, the strategy the live path can send):")
    for c in rep["scorecard"]:
        print(f"  {c['code']:4} {c['result']:12} {c['criterion']}")
        print(f"       {c['detail']}")
    print()
    print(f"VERDICT: {rep['verdict']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("logs", help="a run directory or the log_dir that holds them (e.g. logs/measure7d)")
    ap.add_argument("--scan-log", type=Path, help="the scan's stderr (gross counts, disconnects, halts)")
    ap.add_argument("--rtt-log", type=Path, help="output of scripts/rtt_probe.sh")
    ap.add_argument("--days", type=int, default=7, help="complete UTC days the scorecard needs (default 7)")
    ap.add_argument("--cooldown-s", type=float, default=2.0, help="risk.cooldown_s used in the run (default 2)")
    ap.add_argument("--capital-usd", type=float, default=1000.0,
                    help="capital in play for the drawdown test: the paper starting quote (default 1000) "
                         "or live.capital_usd for a live run's logs")
    ap.add_argument("--min-day-coverage", type=float, default=0.95,
                    help="fraction of a UTC day the runs must have been up for it to count as complete (default 0.95)")
    ap.add_argument("--include-today", action="store_true", help="score the current, incomplete UTC day too")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)
    root = Path(args.logs)
    if not find_runs(root):
        print(f"no opportunities/trades/anomalies JSONL found under {root}", file=sys.stderr)
        return 2
    for p in (args.scan_log, args.rtt_log):
        if p is not None and not p.exists():
            print(f"missing file: {p}", file=sys.stderr)
            return 2
    rep = build_report(root, args.days, args.cooldown_s, args.capital_usd, args.include_today, args.scan_log, args.rtt_log,
                       min_day_coverage=args.min_day_coverage)
    if args.json:
        print(json.dumps(_jsonable(rep), indent=2, default=str, allow_nan=False))
    else:
        print_report(rep)
    return 0


if __name__ == "__main__":
    sys.exit(main())
