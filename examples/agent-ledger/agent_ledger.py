#!/usr/bin/env python3
"""agent-ledger: measure human-agent interaction time and the work agents produce.

Reads the session logs that agents already write on this machine and turns them
into a daily ledger. No network, no API keys, standard library only (Python 3.9+,
so it runs on the python3 that ships with macOS).

Sources
  Claude Code   ~/.claude/projects/**/*.jsonl   (local sessions, including ones
                                                driven from the phone or desktop
                                                app through Remote Control)
  Codex         ~/.codex/sessions/**/*.jsonl     (experimental; see README)

Usage
  python3 agent_ledger.py collect [--since YYYY-MM-DD] [--until YYYY-MM-DD]
  python3 agent_ledger.py report  [--days 7]

What it measures, per session per local day (definitions in README.md):
  human_attention_min  time Jake was engaged: the gap between an agent finishing
                       and his next message, when that gap is short enough that
                       he was plausibly reading and replying (default <= 15 min);
                       longer gaps count a flat "return" allowance (2 min)
  agent_active_min     time agents spent working, summed across sessions
  agent_wait_min       time a turn sat idle mid-way (usually a permission prompt
                       or a long-running command), split out so it doesn't
                       inflate active time
  subagent_min         time spent by sub-agents the session spawned
  outputs              counts of concrete actions: files edited, commits, PRs,
                       documents written, messages drafted or sent, posts
                       queued, calendar events, research calls, hand-offs to
                       other agents
"""

import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    sys.exit("agent-ledger needs Python 3.9 or newer (for zoneinfo).")

DEFAULT_TZ = "Australia/Perth"
DEFAULT_OUT = "~/AF_OS/metrics/agent-ledger"
ENGAGED_GAP_MIN = 15.0   # a reply within this many minutes counts the whole gap
RETURN_ALLOWANCE_MIN = 2.0  # a reply after a longer gap counts this much
IDLE_CAP_MIN = 10.0      # a gap this long inside a turn is waiting, not working

OUTPUT_COLUMNS = [
    "files_edited", "commits", "pushes", "prs_opened", "prs_merged",
    "docs_written", "messages_drafted", "messages_sent", "posts_queued",
    "calendar_events", "research_calls", "agent_handoffs", "tool_calls",
]

SESSION_COLUMNS = [
    "date", "source", "session_id", "title", "project", "entrypoint", "models",
    "human_prompts", "agent_prompts", "turns",
    "human_attention_min", "agent_active_min", "agent_wait_min", "subagent_min",
    "output_tokens", "input_tokens", "cost_usd", "lines_added", "lines_removed",
] + OUTPUT_COLUMNS

DAILY_COLUMNS = [
    "date", "sessions", "human_prompts", "agent_prompts",
    "human_min", "agent_min", "agent_wall_min", "subagent_min",
    "agent_min_per_human_hour", "outputs_total", "outputs_per_human_hour",
    "cost_usd", "output_tokens",
] + OUTPUT_COLUMNS


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def parse_ts(value):
    """ISO-8601 string (with Z or offset) -> aware UTC datetime, or None."""
    if not value or not isinstance(value, str):
        return None
    s = value.strip().replace("Z", "+00:00")
    # Python 3.9's fromisoformat takes only 3 or 6 fractional digits; logs vary.
    s = _FRACTION.sub(lambda m: "." + (m.group(1) + "000000")[:6], s)
    try:
        parsed = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


_FRACTION = re.compile(r"\.(\d+)(?=[+-]\d\d:\d\d$|$)")


def minutes(td):
    return td.total_seconds() / 60.0


def read_jsonl(path):
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return out


def union_minutes(intervals):
    """Total minutes covered by a list of (start, end) datetimes, overlaps merged."""
    spans = sorted((s, e) for s, e in intervals if e > s)
    total, cur_s, cur_e = 0.0, None, None
    for s, e in spans:
        if cur_e is None or s > cur_e:
            if cur_e is not None:
                total += minutes(cur_e - cur_s)
            cur_s, cur_e = s, e
        else:
            cur_e = max(cur_e, e)
    if cur_e is not None:
        total += minutes(cur_e - cur_s)
    return total


def split_by_day(start, end, tz):
    """Split a UTC interval at local midnights -> [(local_date, start, end)]."""
    pieces = []
    s = start
    while s < end:
        local = s.astimezone(tz)
        next_midnight = (local + dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        e = min(end, next_midnight.astimezone(dt.timezone.utc))
        pieces.append((local.date(), s, e))
        s = e
    return pieces


# --------------------------------------------------------------------------
# Classifying prompts and tool calls
# --------------------------------------------------------------------------

def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def is_tool_result(content):
    return isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content
    )


_SYSTEM_PREFIXES = (
    "<local-command-stdout>", "<local-command-stderr>", "<local-command-caveat>",
    "<bash-stdout>", "<bash-stderr>", "<system-reminder>", "Caveat:",
)
_AGENT_PREFIXES = ("<task-notification>", "<webhook-payload>", "<cross-session-message",
                   "<other-session", "<scheduled-task", "<child-session-event")


def classify_prompt(rec):
    """Return 'human', 'agent', 'system' or None (not a prompt) for a user record."""
    if rec.get("type") != "user":
        return None
    msg = rec.get("message") or {}
    content = msg.get("content")
    if is_tool_result(content):
        return None
    origin = rec.get("origin")
    if isinstance(origin, dict) and origin.get("kind"):
        kind = str(origin["kind"]).lower()
        return "human" if kind == "human" else "agent"
    turn_origin = rec.get("turnOrigin")
    if isinstance(turn_origin, str) and turn_origin:
        return "human" if turn_origin.lower() == "human" else "agent"
    if rec.get("isMeta"):
        return "system"
    text = text_of(content).lstrip()
    if not text:
        return None
    if text.startswith(_SYSTEM_PREFIXES):
        return "system"
    if text.startswith(_AGENT_PREFIXES):
        return "agent"
    # Older transcripts carry no origin field: a plain typed message, a slash
    # command or "[Request interrupted by user]" all come from the person.
    return "human"


_DOC_PATTERNS = (
    "notion-create", "notion-update", "claude_docs__batch", "claude_docs__update",
    "claude_docs__create", "google_docs__update", "google_drive__create",
    "google_drive__update", "google_sheets__update", "google_sheets__append",
    "google_slides__update", "__save_post", "__edit_post", "__save_page",
)
# Work handed to another agent: sub-agents, workflows, other sessions. Matched
# on the remote-session server's own names so Gmail's send_message stays an email.
_HANDOFF_NAMES = {"agent", "task", "workflow", "sendmessage"}
_HANDOFF_PATTERNS = ("claude-code-remote__create_session", "claude-code-remote__send_message",
                     "claude-code-remote__fire_trigger", "__spawn_task")


def classify_tool(name, tool_input, counts, files):
    """Add one tool call to the output counters."""
    n = (name or "").lower()
    inp = tool_input if isinstance(tool_input, dict) else {}
    counts["tool_calls"] += 1

    if n in ("edit", "write", "multiedit", "notebookedit"):
        path = inp.get("file_path") or inp.get("notebook_path")
        if path:
            files.add(path)
        return
    if n == "bash":
        cmd = str(inp.get("command", ""))
        if re.search(r"\bgit\s+commit\b", cmd):
            counts["commits"] += 1
        if re.search(r"\bgit\s+push\b", cmd):
            counts["pushes"] += 1
        if re.search(r"\bgh\s+pr\s+create\b", cmd):
            counts["prs_opened"] += 1
        if re.search(r"\bgh\s+pr\s+merge\b", cmd):
            counts["prs_merged"] += 1
        return
    if n == "artifact":
        if inp.get("action") in (None, "publish"):
            counts["docs_written"] += 1
        return
    if n in ("websearch", "webfetch") or "firecrawl_search" in n or "firecrawl_scrape" in n:
        counts["research_calls"] += 1
        return
    if n in _HANDOFF_NAMES or any(p in n for p in _HANDOFF_PATTERNS):
        counts["agent_handoffs"] += 1
        return
    if "create_pull_request" in n:
        counts["prs_opened"] += 1
    elif "merge_pull_request" in n:
        counts["prs_merged"] += 1
    elif "push_files" in n or "create_or_update_file" in n:
        counts["commits"] += 1
    elif "gmail__create_draft" in n or "gmail__update_draft" in n:
        counts["messages_drafted"] += 1
    elif "gmail__send" in n or "gmail__reply" in n or "gmail__forward" in n:
        counts["messages_sent"] += 1
    elif "createscheduledpost" in n:  # Metricool, with or without review
        counts["posts_queued"] += 1
    elif "calendar" in n and ("create_event" in n or "update_event" in n or "create-events" in n):
        counts["calendar_events"] += 1
    elif any(p in n for p in _DOC_PATTERNS):
        # one notion-create-pages call can create many pages; count each
        pages = inp.get("pages") if "notion-create-pages" in n else None
        counts["docs_written"] += len(pages) if isinstance(pages, list) and pages else 1


# --------------------------------------------------------------------------
# A parsed session
# --------------------------------------------------------------------------

class Session:
    """Everything the ledger needs from one session log, split by local day."""

    def __init__(self, source, session_id, tz):
        self.source = source
        self.session_id = session_id
        self.tz = tz
        self.failed_ids = set()
        self.title = ""
        self.project = ""
        self.entrypoint = ""
        self.models = set()
        # per local date
        self.human_prompts = Counter()
        self.agent_prompts = Counter()
        self.turns = Counter()
        self.active = defaultdict(float)
        self.wait = defaultdict(float)
        self.subagent = defaultdict(float)
        self.output_tokens = Counter()
        self.input_tokens = Counter()
        self.counts = defaultdict(Counter)
        self.files = defaultdict(set)
        self.cost = defaultdict(float)
        self.lines_added = Counter()
        self.lines_removed = Counter()
        # (prompt_time_utc, previous_turn_end_utc or None) for human prompts
        self.human_events = []
        # agent activity intervals, for wall-clock union across sessions
        self.active_intervals = []

    def day(self, ts):
        return ts.astimezone(self.tz).date()

    def add_active(self, start, end, bucket=None):
        bucket = self.active if bucket is None else bucket
        for d, s, e in split_by_day(start, end, self.tz):
            bucket[d] += minutes(e - s)
        if bucket is self.active:
            self.active_intervals.append((start, end))

    def dates(self):
        keys = set(self.human_prompts) | set(self.agent_prompts) | set(self.active) | set(self.counts)
        return sorted(keys)

    def row(self, d):
        c = self.counts[d]
        row = {
            "date": d.isoformat(),
            "source": self.source,
            "session_id": self.session_id,
            "title": self.title,
            "project": self.project,
            "entrypoint": self.entrypoint,
            "models": " ".join(sorted(self.models)),
            "human_prompts": self.human_prompts[d],
            "agent_prompts": self.agent_prompts[d],
            "turns": self.turns[d],
            "human_attention_min": round(self.human_minutes_by_day().get(d, 0.0), 1),
            "agent_active_min": round(self.active[d], 1),
            "agent_wait_min": round(self.wait[d], 1),
            "subagent_min": round(self.subagent[d], 1),
            "output_tokens": self.output_tokens[d],
            "input_tokens": self.input_tokens[d],
            "cost_usd": round(self.cost[d], 4),
            "lines_added": self.lines_added[d],
            "lines_removed": self.lines_removed[d],
        }
        for col in OUTPUT_COLUMNS:
            row[col] = c[col]
        row["files_edited"] = len(self.files[d])
        return row

    def human_intervals(self, engaged=None, allowance=None):
        engaged = ENGAGED_GAP_MIN if engaged is None else engaged
        allowance = RETURN_ALLOWANCE_MIN if allowance is None else allowance
        out = []
        for prompt_at, prev_end in self.human_events:
            if prev_end is not None and dt.timedelta(0) <= prompt_at - prev_end <= dt.timedelta(minutes=engaged):
                out.append((prev_end, prompt_at))
            else:
                out.append((prompt_at - dt.timedelta(minutes=allowance), prompt_at))
        return out

    def human_minutes_by_day(self):
        per = defaultdict(float)
        for s, e in self.human_intervals():
            for d, ps, pe in split_by_day(s, e, self.tz):
                per[d] += minutes(pe - ps)
        return per


# --------------------------------------------------------------------------
# Claude Code transcripts
# --------------------------------------------------------------------------

# Bookkeeping records that say nothing about when work happened. "attachment"
# records are context injected alongside a prompt and can be stamped a moment
# before it, which would otherwise glue an overnight gap onto the previous turn.
_SKIP_TIMING = {"atis-latch", "last-prompt", "ai-title", "summary", "mode", "tools-used",
                "cost-state", "queue-operation", "custom-title", "tag", "attachment"}


def failed_tool_ids(records):
    """tool_use ids whose result came back as an error (they produced nothing)."""
    failed = set()
    for rec in records:
        if rec.get("type") != "user":
            continue
        content = (rec.get("message") or {}).get("content")
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("is_error"):
                    if b.get("tool_use_id"):
                        failed.add(b["tool_use_id"])
    return failed


def _active_between(times, idle_cap):
    """Split consecutive gaps into (active_pairs, wait_minutes)."""
    active, wait = [], 0.0
    for a, b in zip(times, times[1:]):
        gap = b - a
        if gap <= dt.timedelta(0):
            continue
        if gap <= dt.timedelta(minutes=idle_cap):
            active.append((a, b))
        else:
            wait += minutes(gap)
    return active, wait


def parse_claude_session(main_path, tz, idle_cap=None):
    idle_cap = IDLE_CAP_MIN if idle_cap is None else idle_cap
    records = read_jsonl(main_path)
    if not records:
        return None
    session_id = Path(main_path).stem
    sess = Session("claude-code", session_id, tz)
    sess.failed_ids = failed_tool_ids(records)

    for rec in records:
        t = rec.get("type")
        if t == "ai-title" and rec.get("aiTitle"):
            sess.title = rec["aiTitle"]
        elif t == "custom-title" and rec.get("customTitle"):
            sess.title = rec["customTitle"]
        elif t == "summary" and rec.get("summary") and not sess.title:
            sess.title = rec["summary"]
        if not sess.project and rec.get("cwd"):
            sess.project = rec["cwd"]
        if not sess.entrypoint and rec.get("entrypoint"):
            sess.entrypoint = rec["entrypoint"]

    main = [r for r in records if not r.get("isSidechain")]
    side = [r for r in records if r.get("isSidechain")]

    # ---- turns, prompts and active time (main thread) ----
    timed = []
    for rec in main:
        if rec.get("type") in _SKIP_TIMING:
            continue
        ts = parse_ts(rec.get("timestamp"))
        if ts is not None:
            timed.append((ts, rec))
    timed.sort(key=lambda x: x[0])

    turn_times = []
    last_turn_end = None
    seen_prompt_ids = set()

    def close_turn():
        nonlocal turn_times, last_turn_end
        if turn_times:
            pairs, wait = _active_between(turn_times, idle_cap)
            for a, b in pairs:
                sess.add_active(a, b)
            sess.wait[sess.day(turn_times[0])] += wait
            last_turn_end = turn_times[-1]
        turn_times = []

    for ts, rec in timed:
        kind = classify_prompt(rec)
        if kind in ("human", "agent"):
            key = rec.get("promptId") or rec.get("uuid")
            if key in seen_prompt_ids:
                turn_times.append(ts)
                continue
            seen_prompt_ids.add(key)
            close_turn()
            d = sess.day(ts)
            sess.turns[d] += 1
            if kind == "human":
                sess.human_prompts[d] += 1
                sess.human_events.append((ts, last_turn_end))
            else:
                sess.agent_prompts[d] += 1
            turn_times = [ts]
            continue
        if turn_times:
            turn_times.append(ts)
        if rec.get("type") == "assistant":
            _count_assistant(sess, rec, ts)
    close_turn()

    # ---- sub-agents inline in the transcript ----
    _add_subagent_time(sess, side, idle_cap)

    # ---- sub-agent transcripts in <session>/subagents/*.jsonl ----
    sub_dir = Path(main_path).with_suffix("") / "subagents"
    if sub_dir.is_dir():
        for sub in sorted(sub_dir.glob("*.jsonl")):
            _add_subagent_time(sess, read_jsonl(sub), idle_cap, count_outputs=True)

    # ---- cost and lines from cumulative cost-state records ----
    _cost_by_day(sess, records)
    return sess


def _count_assistant(sess, rec, ts):
    msg = rec.get("message") or {}
    d = sess.day(ts)
    if msg.get("model") and not str(msg["model"]).startswith("<"):
        sess.models.add(msg["model"])
    usage = msg.get("usage") or {}
    sess.output_tokens[d] += int(usage.get("output_tokens") or 0)
    sess.input_tokens[d] += sum(int(usage.get(k) or 0) for k in (
        "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    content = msg.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                if block.get("id") in sess.failed_ids:
                    sess.counts[d]["tool_calls"] += 1  # attempted, produced nothing
                    continue
                classify_tool(block.get("name"), block.get("input"), sess.counts[d], sess.files[d])


def _add_subagent_time(sess, records, idle_cap, count_outputs=False):
    if count_outputs:
        sess.failed_ids |= failed_tool_ids(records)
    times = []
    for rec in records:
        ts = parse_ts(rec.get("timestamp"))
        if ts is None or rec.get("type") in _SKIP_TIMING:
            continue
        times.append(ts)
        if count_outputs and rec.get("type") == "assistant":
            _count_assistant(sess, rec, ts)
    times.sort()
    pairs, _ = _active_between(times, idle_cap)
    for a, b in pairs:
        sess.add_active(a, b, bucket=sess.subagent)


def _cost_by_day(sess, records):
    """cost-state records are running totals; attribute each increase to its day."""
    states = []
    last_ts = None
    for rec in records:
        ts = parse_ts(rec.get("timestamp"))
        if ts is not None:
            last_ts = ts
        if rec.get("type") == "cost-state" and last_ts is not None:
            states.append((last_ts, rec))
    prev_cost = prev_add = prev_rem = 0.0
    prev_start = None
    for ts, st in states:
        start = st.get("startTime")
        if start != prev_start:  # a restarted process resets the totals
            prev_cost = prev_add = prev_rem = 0.0
            prev_start = start
        cost = float(st.get("totalCostUSD") or 0)
        add = float(st.get("totalLinesAdded") or 0)
        rem = float(st.get("totalLinesRemoved") or 0)
        d = sess.day(ts)
        sess.cost[d] += max(0.0, cost - prev_cost)
        sess.lines_added[d] += int(max(0.0, add - prev_add))
        sess.lines_removed[d] += int(max(0.0, rem - prev_rem))
        prev_cost, prev_add, prev_rem = max(prev_cost, cost), max(prev_add, add), max(prev_rem, rem)


def iter_claude_sessions(root, tz, since_utc):
    root = Path(os.path.expanduser(root))
    if not root.is_dir():
        return
    for path in sorted(root.glob("*/*.jsonl")):
        try:
            if since_utc and dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc) < since_utc:
                continue
        except OSError:
            continue
        sess = parse_claude_session(path, tz)
        if sess is not None:
            yield sess


# --------------------------------------------------------------------------
# Codex (experimental)
# --------------------------------------------------------------------------

def parse_codex_session(path, tz, idle_cap=None):
    """Best-effort reader for Codex rollout logs. See README for its limits."""
    idle_cap = IDLE_CAP_MIN if idle_cap is None else idle_cap
    records = read_jsonl(path)
    if not records:
        return None
    sess = Session("codex", Path(path).stem, tz)
    timed = []
    last_tokens = {}
    for rec in records:
        ts = parse_ts(rec.get("timestamp"))
        payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
        if rec.get("type") == "session_meta":
            sess.project = payload.get("cwd", "") or sess.project
            sess.entrypoint = "codex"
        if ts is None:
            continue
        ptype = payload.get("type")
        if rec.get("type") == "event_msg" and ptype == "token_count":
            info = payload.get("info") or {}
            total = info.get("total_token_usage") or {}
            last_tokens[sess.day(ts)] = int(total.get("output_tokens") or 0)
        is_human = rec.get("type") == "event_msg" and ptype == "user_message"
        timed.append((ts, is_human, rec, payload))
    timed.sort(key=lambda x: x[0])

    turn_times, last_end = [], None
    for ts, is_human, rec, payload in timed:
        if is_human:
            if turn_times:
                pairs, wait = _active_between(turn_times, idle_cap)
                for a, b in pairs:
                    sess.add_active(a, b)
                sess.wait[sess.day(turn_times[0])] += wait
                last_end = turn_times[-1]
            d = sess.day(ts)
            sess.turns[d] += 1
            sess.human_prompts[d] += 1
            sess.human_events.append((ts, last_end))
            turn_times = [ts]
            continue
        if turn_times:
            turn_times.append(ts)
        if rec.get("type") == "response_item" and payload.get("type") in ("function_call", "custom_tool_call"):
            name = payload.get("name", "")
            args = payload.get("arguments")
            try:
                args = json.loads(args) if isinstance(args, str) else (args or {})
            except json.JSONDecodeError:
                args = {}
            if name in ("shell", "exec_command") and isinstance(args, dict):
                cmd = args.get("command") or args.get("cmd")
                args = {"command": " ".join(cmd) if isinstance(cmd, list) else str(cmd or "")}
                name = "bash"
            classify_tool(name, args, sess.counts[sess.day(ts)], sess.files[sess.day(ts)])
    if turn_times:
        pairs, wait = _active_between(turn_times, idle_cap)
        for a, b in pairs:
            sess.add_active(a, b)
        sess.wait[sess.day(turn_times[0])] += wait
    # token_count totals are cumulative for the whole session
    prev = 0
    for d in sorted(last_tokens):
        sess.output_tokens[d] = max(0, last_tokens[d] - prev)
        prev = max(prev, last_tokens[d])
    return sess


def iter_codex_sessions(root, tz, since_utc):
    root = Path(os.path.expanduser(root))
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*.jsonl")):
        try:
            if since_utc and dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc) < since_utc:
                continue
        except OSError:
            continue
        sess = parse_codex_session(path, tz)
        if sess is not None:
            yield sess


# --------------------------------------------------------------------------
# Building the ledger
# --------------------------------------------------------------------------

def build(sessions, since=None, until=None):
    """-> (session_rows, daily_rows) for local dates within [since, until]."""
    def in_range(d):
        return (since is None or d >= since) and (until is None or d <= until)

    session_rows = []
    human_iv, agent_iv = defaultdict(list), defaultdict(list)
    for sess in sessions:
        for d in sess.dates():
            if in_range(d):
                session_rows.append(sess.row(d))
        for s, e in sess.human_intervals():
            for d, ps, pe in split_by_day(s, e, sess.tz):
                if in_range(d):
                    human_iv[d].append((ps, pe))
        for s, e in sess.active_intervals:
            for d, ps, pe in split_by_day(s, e, sess.tz):
                if in_range(d):
                    agent_iv[d].append((ps, pe))
    session_rows.sort(key=lambda r: (r["date"], r["source"], r["session_id"]))
    return session_rows, daily_from_rows(session_rows, human_iv, agent_iv)


def daily_from_rows(session_rows, human_iv=None, agent_iv=None):
    by_day = defaultdict(list)
    for r in session_rows:
        by_day[r["date"]].append(r)
    daily = []
    for day in sorted(by_day):
        rows = by_day[day]
        d = dt.date.fromisoformat(day)
        num = lambda col: sum(float(r.get(col) or 0) for r in rows)
        if human_iv is not None and d in human_iv:
            human = union_minutes(human_iv[d])
        else:  # rows loaded from an old ledger: no intervals, so sum
            human = num("human_attention_min")
        if agent_iv is not None and d in agent_iv:
            wall = union_minutes(agent_iv[d])
        else:
            wall = num("agent_active_min")
        agent = num("agent_active_min")
        outputs_total = sum(num(c) for c in OUTPUT_COLUMNS if c not in ("tool_calls", "research_calls"))
        row = {
            "date": day,
            "sessions": len(rows),
            "human_prompts": int(num("human_prompts")),
            "agent_prompts": int(num("agent_prompts")),
            "human_min": round(human, 1),
            "agent_min": round(agent, 1),
            "agent_wall_min": round(wall, 1),
            "subagent_min": round(num("subagent_min"), 1),
            "agent_min_per_human_hour": round(agent / human * 60, 1) if human else "",
            "outputs_total": int(outputs_total),
            "outputs_per_human_hour": round(outputs_total / human * 60, 1) if human else "",
            "cost_usd": round(num("cost_usd"), 2),
            "output_tokens": int(num("output_tokens")),
        }
        for c in OUTPUT_COLUMNS:
            row[c] = int(num(c))
        daily.append(row)
    return daily


# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------

def read_csv(path):
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def write_csv(path, columns, rows):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    os.replace(tmp, path)


def merge_rows(existing, fresh):
    """Keep history for sessions whose logs have since been cleaned up."""
    key = lambda r: (r["date"], r["source"], r["session_id"])
    merged = {key(r): r for r in existing}
    for r in fresh:
        merged[key(r)] = r
    return sorted(merged.values(), key=key)


def render_report(daily, session_rows, days):
    daily = daily[-days:]
    if not daily:
        return "# Agent ledger\n\nNo activity recorded yet.\n"
    lines = ["# Agent ledger", "",
             "Human time is an estimate of engaged attention, not a stopwatch. "
             "See README for the definitions.", "",
             "| Date | Sessions | You (min) | Agents (min) | Agent min per your hour | Outputs | Outputs per your hour | Cost (US$) |",
             "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for r in daily:
        lines.append("| {date} | {sessions} | {human_min} | {agent_min} | {agent_min_per_human_hour} | "
                     "{outputs_total} | {outputs_per_human_hour} | {cost_usd} |".format(**r))
    lines += ["", "## Outputs by type", "",
              "| Date | Files | Commits | PRs opened | PRs merged | Docs | Drafts | Sent | Posts | Calendar | Hand-offs |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for r in daily:
        lines.append("| {date} | {files_edited} | {commits} | {prs_opened} | {prs_merged} | {docs_written} | "
                     "{messages_drafted} | {messages_sent} | {posts_queued} | {calendar_events} | {agent_handoffs} |".format(**r))
    shown = {r["date"] for r in daily}
    top = sorted((r for r in session_rows if r["date"] in shown),
                 key=lambda r: -float(r.get("agent_active_min") or 0))[:10]
    if top:
        lines += ["", "## Busiest sessions", "",
                  "| Date | Session | You (min) | Agent (min) | Waiting (min) | Prompts | Outputs |",
                  "| --- | --- | --- | --- | --- | --- | --- |"]
        for r in top:
            outs = sum(int(float(r.get(c) or 0)) for c in OUTPUT_COLUMNS if c not in ("tool_calls", "research_calls"))
            title = (r.get("title") or r["session_id"][:8]).replace("|", "/")
            lines.append(f"| {r['date']} | {title} | {r['human_attention_min']} | {r['agent_active_min']} | "
                         f"{r['agent_wait_min']} | {r['human_prompts']} | {outs} |")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv=None):
    global ENGAGED_GAP_MIN, RETURN_ALLOWANCE_MIN, IDLE_CAP_MIN
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("collect", "report"):
        sp = sub.add_parser(name)
        sp.add_argument("--out", default=os.environ.get("AGENT_LEDGER_OUT", DEFAULT_OUT))
        sp.add_argument("--tz", default=DEFAULT_TZ)
        sp.add_argument("--days", type=int, default=7, help="days shown in report.md")
    c = sub.choices["collect"]
    c.add_argument("--since", help="first local date to (re)compute, YYYY-MM-DD (default: 35 days ago)")
    c.add_argument("--until", help="last local date to compute, YYYY-MM-DD (default: today)")
    c.add_argument("--claude-dir", default="~/.claude/projects")
    c.add_argument("--codex-dir", default="~/.codex/sessions")
    c.add_argument("--no-codex", action="store_true")
    c.add_argument("--print", action="store_true", help="print the report after collecting")
    c.add_argument("--engaged-gap", type=float, default=ENGAGED_GAP_MIN,
                   help="minutes: a reply this soon after an agent finishes counts the whole gap")
    c.add_argument("--return-allowance", type=float, default=RETURN_ALLOWANCE_MIN,
                   help="minutes credited for a reply after a longer gap")
    c.add_argument("--idle-cap", type=float, default=IDLE_CAP_MIN,
                   help="minutes: a silent gap this long inside a turn counts as waiting")
    args = p.parse_args(argv)

    tz = ZoneInfo(args.tz)
    if args.cmd == "collect":
        ENGAGED_GAP_MIN, RETURN_ALLOWANCE_MIN, IDLE_CAP_MIN = (
            args.engaged_gap, args.return_allowance, args.idle_cap)
    out = Path(os.path.expanduser(args.out))
    out.mkdir(parents=True, exist_ok=True)
    sessions_csv, daily_csv, report_md = out / "sessions.csv", out / "daily.csv", out / "report.md"

    if args.cmd == "report":
        rows = read_csv(sessions_csv)
        print(render_report(read_csv(daily_csv), rows, args.days))
        return 0

    today = dt.datetime.now(tz).date()
    since = dt.date.fromisoformat(args.since) if args.since else today - dt.timedelta(days=35)
    until = dt.date.fromisoformat(args.until) if args.until else today
    since_utc = dt.datetime.combine(since, dt.time(), tz).astimezone(dt.timezone.utc)

    sessions = list(iter_claude_sessions(args.claude_dir, tz, since_utc))
    if not args.no_codex:
        sessions += list(iter_codex_sessions(args.codex_dir, tz, since_utc))

    fresh_rows, fresh_daily = build(sessions, since, until)
    rows = merge_rows(read_csv(sessions_csv), fresh_rows)
    # Days computed this run use exact interval unions. Older days keep the row
    # they were given when their logs still existed; a day with no saved row is
    # rebuilt from its session rows (human time summed, so an upper bound).
    fresh_dates = {r["date"] for r in fresh_daily}
    saved = {r["date"]: r for r in read_csv(daily_csv) if r["date"] not in fresh_dates}
    missing = {r["date"] for r in rows} - fresh_dates - set(saved)
    rebuilt = daily_from_rows([r for r in rows if r["date"] in missing])
    daily = sorted(fresh_daily + list(saved.values()) + rebuilt, key=lambda r: r["date"])

    write_csv(sessions_csv, SESSION_COLUMNS, rows)
    write_csv(daily_csv, DAILY_COLUMNS, daily)
    report = render_report(daily, rows, args.days)
    report_md.write_text(report, encoding="utf-8")
    print(f"agent-ledger: {len(sessions)} session logs read, {len(fresh_rows)} session-days "
          f"from {since} to {until}; ledger at {out}")
    if args.print:
        print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
