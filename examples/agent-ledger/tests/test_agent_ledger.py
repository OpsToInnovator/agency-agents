"""Tests for agent_ledger. Run: python3 -m unittest discover -s tests"""

import csv
import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import agent_ledger as al  # noqa: E402

PERTH = ZoneInfo("Australia/Perth")
DAY = dt.date(2026, 11, 17)


def at(hh, mm, ss=0, day=DAY):
    """Perth wall-clock time -> ISO UTC string as Claude Code writes it."""
    local = dt.datetime.combine(day, dt.time(hh, mm, ss), PERTH)
    return local.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def human(ts, text="do the thing", pid=None):
    return {"type": "user", "timestamp": ts, "origin": {"kind": "human"}, "turnOrigin": "human",
            "promptId": pid or f"p-{ts}", "uuid": f"u-{ts}", "message": {"role": "user", "content": text}}


def agent_prompt(ts, text="<task-notification>done</task-notification>"):
    return {"type": "user", "timestamp": ts, "origin": {"kind": "task-notification"},
            "promptId": f"a-{ts}", "uuid": f"ua-{ts}", "message": {"role": "user", "content": text}}


def assistant(ts, tools=(), out_tokens=100, model="claude-test"):
    content = [{"type": "text", "text": "ok"}]
    for i, (name, inp) in enumerate(tools):
        content.append({"type": "tool_use", "id": f"t-{ts}-{i}", "name": name, "input": inp})
    return {"type": "assistant", "timestamp": ts, "uuid": f"as-{ts}",
            "message": {"role": "assistant", "model": model, "content": content,
                        "usage": {"input_tokens": 10, "output_tokens": out_tokens,
                                  "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}}


def tool_result(ts, tool_id, error=False):
    return {"type": "user", "timestamp": ts, "uuid": f"tr-{ts}",
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tool_id, "is_error": error, "content": "x"}]}}


def write_session(dirpath, name, records):
    proj = Path(dirpath) / "proj"
    proj.mkdir(parents=True, exist_ok=True)
    path = proj / f"{name}.jsonl"
    with open(path, "w") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")
    return path


def read_rows(path):
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


def parse(records, name="s1"):
    with tempfile.TemporaryDirectory() as d:
        return al.parse_claude_session(write_session(d, name, records), PERTH)


class ClassifyPrompt(unittest.TestCase):
    def test_origin_field_wins(self):
        self.assertEqual(al.classify_prompt(human(at(9, 0))), "human")
        self.assertEqual(al.classify_prompt(agent_prompt(at(9, 0))), "agent")

    def test_tool_results_are_not_prompts(self):
        self.assertIsNone(al.classify_prompt(tool_result(at(9, 0), "t1")))

    def test_legacy_records_without_origin(self):
        base = {"type": "user", "timestamp": at(9, 0), "message": {"role": "user"}}
        typed = dict(base, message={"role": "user", "content": "hello"})
        caveat = dict(base, message={"role": "user", "content": "<local-command-stdout>x</local-command-stdout>"})
        notif = dict(base, message={"role": "user", "content": "<task-notification>x</task-notification>"})
        meta = dict(base, isMeta=True, message={"role": "user", "content": "anything"})
        self.assertEqual(al.classify_prompt(typed), "human")
        self.assertEqual(al.classify_prompt(caveat), "system")
        self.assertEqual(al.classify_prompt(notif), "agent")
        self.assertEqual(al.classify_prompt(meta), "system")


class Timing(unittest.TestCase):
    def test_active_time_and_engaged_reply(self):
        s = parse([
            human(at(9, 0)),
            assistant(at(9, 1)),
            assistant(at(9, 4)),          # turn ends 09:04, 4 min of work
            human(at(9, 9)),              # replied 5 min later: engaged
            assistant(at(9, 12)),         # 3 min of work
        ])
        row = s.row(DAY)
        self.assertEqual(row["human_prompts"], 2)
        self.assertEqual(row["turns"], 2)
        self.assertAlmostEqual(row["agent_active_min"], 7.0)
        # first prompt: 2 min allowance; second: the 5 min gap
        self.assertAlmostEqual(row["human_attention_min"], 7.0)

    def test_long_gap_counts_only_the_return_allowance(self):
        s = parse([human(at(9, 0)), assistant(at(9, 2)), human(at(13, 0)), assistant(at(13, 1))])
        self.assertAlmostEqual(s.row(DAY)["human_attention_min"], 4.0)

    def test_idle_gap_inside_a_turn_is_waiting(self):
        s = parse([human(at(9, 0)), assistant(at(9, 2)), assistant(at(9, 32)), assistant(at(9, 33))])
        row = s.row(DAY)
        self.assertAlmostEqual(row["agent_active_min"], 3.0)
        self.assertAlmostEqual(row["agent_wait_min"], 30.0)

    def test_attachment_before_a_prompt_does_not_glue_turns(self):
        attach = {"type": "attachment", "timestamp": at(21, 59, 59), "uuid": "att"}
        s = parse([human(at(9, 0)), assistant(at(9, 2)), attach, human(at(22, 0)), assistant(at(22, 1))])
        self.assertAlmostEqual(s.row(DAY)["agent_wait_min"], 0.0)

    def test_split_at_local_midnight(self):
        s = parse([human(at(23, 55)), assistant(at(23, 58)),
                   assistant(at(0, 3, day=DAY + dt.timedelta(days=1)))])
        self.assertAlmostEqual(s.row(DAY)["agent_active_min"], 5.0)
        self.assertAlmostEqual(s.row(DAY + dt.timedelta(days=1))["agent_active_min"], 3.0)

    def test_agent_prompts_counted_separately(self):
        s = parse([human(at(9, 0)), assistant(at(9, 1)), agent_prompt(at(9, 30)), assistant(at(9, 31))])
        row = s.row(DAY)
        self.assertEqual(row["human_prompts"], 1)
        self.assertEqual(row["agent_prompts"], 1)
        self.assertEqual(row["turns"], 2)


class Outputs(unittest.TestCase):
    def test_counts_by_type(self):
        s = parse([
            human(at(9, 0)),
            assistant(at(9, 1), tools=[
                ("Edit", {"file_path": "/a.py"}), ("Edit", {"file_path": "/a.py"}),
                ("Write", {"file_path": "/b.md"}),
                ("Bash", {"command": "git add . && git commit -m x && git push -u origin b"}),
                ("Bash", {"command": "gh pr create --draft"}),
                ("mcp__github__merge_pull_request", {}),
                ("mcp__Notion__notion-create-pages", {"pages": [{}, {}, {}]}),
                ("mcp__Notion__notion-update-page", {}),
                ("Artifact", {"action": "publish"}), ("Artifact", {"action": "read"}),
                ("mcp__Gmail__create_draft", {}), ("mcp__Gmail__send_message", {}),
                ("mcp__Metricool_Social_Media_Management__createScheduledPostForReview", {}),
                ("mcp__Google_Calendar__create_event", {}),
                ("WebSearch", {}), ("Agent", {}), ("mcp__claude-code-remote__send_message", {}),
            ]),
        ])
        r = s.row(DAY)
        self.assertEqual(r["files_edited"], 2)
        self.assertEqual(r["commits"], 1)
        self.assertEqual(r["pushes"], 1)
        self.assertEqual(r["prs_opened"], 1)
        self.assertEqual(r["prs_merged"], 1)
        self.assertEqual(r["docs_written"], 5)  # 3 pages + 1 update + 1 artifact
        self.assertEqual(r["messages_drafted"], 1)
        self.assertEqual(r["messages_sent"], 1)
        self.assertEqual(r["posts_queued"], 1)
        self.assertEqual(r["calendar_events"], 1)
        self.assertEqual(r["research_calls"], 1)
        self.assertEqual(r["agent_handoffs"], 2)
        self.assertEqual(r["output_tokens"], 100)

    def test_failed_calls_produce_nothing(self):
        a = assistant(at(9, 1), tools=[("mcp__Google_Calendar__create_event", {}),
                                       ("mcp__Google_Calendar__create_event", {})])
        ok_id, bad_id = (b["id"] for b in a["message"]["content"] if b["type"] == "tool_use")
        s = parse([human(at(9, 0)), a, tool_result(at(9, 1, 5), ok_id),
                   tool_result(at(9, 1, 6), bad_id, error=True)])
        r = s.row(DAY)
        self.assertEqual(r["calendar_events"], 1)
        self.assertEqual(r["tool_calls"], 2)

    def test_cost_state_deltas_by_day(self):
        nxt = DAY + dt.timedelta(days=1)
        s = parse([
            human(at(23, 0)), assistant(at(23, 1)),
            {"type": "cost-state", "startTime": 1, "totalCostUSD": 1.5, "totalLinesAdded": 10, "totalLinesRemoved": 2},
            human(at(8, 0, day=nxt)), assistant(at(8, 1, day=nxt)),
            {"type": "cost-state", "startTime": 1, "totalCostUSD": 2.0, "totalLinesAdded": 25, "totalLinesRemoved": 2},
        ])
        self.assertAlmostEqual(s.row(DAY)["cost_usd"], 1.5)
        self.assertAlmostEqual(s.row(nxt)["cost_usd"], 0.5)
        self.assertEqual(s.row(nxt)["lines_added"], 15)


class Ledger(unittest.TestCase):
    def test_human_time_is_not_double_counted_across_parallel_sessions(self):
        # two sessions running side by side, records every 5 minutes
        a = parse([human(at(9, 0)), assistant(at(9, 5)), assistant(at(9, 10)),
                   human(at(9, 15)), assistant(at(9, 20)), assistant(at(9, 25)), assistant(at(9, 30))], "a")
        b = parse([human(at(9, 1)), assistant(at(9, 6)), assistant(at(9, 11)),
                   human(at(9, 14)), assistant(at(9, 19)), assistant(at(9, 24)), assistant(at(9, 29))], "b")
        rows, daily = al.build([a, b])
        self.assertEqual(len(rows), 2)
        d = daily[0]
        per_session = sum(r["human_attention_min"] for r in rows)
        self.assertLess(d["human_min"], per_session)  # union, not sum
        self.assertAlmostEqual(d["agent_min"], 50.0)      # agent-minutes add up
        self.assertAlmostEqual(d["agent_wall_min"], 27.0)  # wall clock: 09:00-09:11 + 09:14-09:30
        self.assertNotEqual(d["agent_min_per_human_hour"], "")

    def test_thresholds_can_be_changed_from_the_command_line(self):
        with tempfile.TemporaryDirectory() as d:
            claude = Path(d) / "claude"
            write_session(claude, "s1", [human(at(9, 0)), assistant(at(9, 15))])
            out = Path(d) / "out"
            try:
                al.main(["collect", "--since", "2026-11-17", "--until", "2026-11-17", "--idle-cap", "20",
                         "--claude-dir", str(claude), "--no-codex", "--out", str(out)])
            finally:
                al.ENGAGED_GAP_MIN, al.RETURN_ALLOWANCE_MIN, al.IDLE_CAP_MIN = 15.0, 2.0, 10.0
            row = read_rows(out / "sessions.csv")[0]
            self.assertEqual(row["agent_active_min"], "15.0")

    def test_collect_writes_files_and_keeps_history(self):
        with tempfile.TemporaryDirectory() as d:
            claude = Path(d) / "claude"
            write_session(claude, "s1", [human(at(9, 0)), assistant(at(9, 5), tools=[("Write", {"file_path": "/x"})])])
            out = Path(d) / "out"
            old = {c: "" for c in al.SESSION_COLUMNS}
            old.update(date="2026-10-01", source="claude-code", session_id="gone", human_prompts="3",
                       human_attention_min="4.0", agent_active_min="12.0", files_edited="2")
            out.mkdir()
            with open(out / "sessions.csv", "w", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=al.SESSION_COLUMNS)
                w.writeheader()
                w.writerow(old)
            rc = al.main(["collect", "--since", "2026-11-17", "--until", "2026-11-17",
                          "--claude-dir", str(claude), "--no-codex", "--out", str(out)])
            self.assertEqual(rc, 0)
            rows = read_rows(out / "sessions.csv")
            self.assertEqual({r["session_id"] for r in rows}, {"gone", "s1"})
            daily = {r["date"]: r for r in read_rows(out / "daily.csv")}
            self.assertIn("2026-10-01", daily)
            self.assertEqual(daily["2026-11-17"]["files_edited"], "1")
            self.assertTrue((out / "report.md").read_text().startswith("# Agent ledger"))


class Codex(unittest.TestCase):
    def test_reads_prompts_time_and_tokens(self):
        recs = [
            {"timestamp": at(9, 0), "type": "session_meta", "payload": {"cwd": "/repo"}},
            {"timestamp": at(9, 0), "type": "event_msg", "payload": {"type": "user_message", "message": "fix it"}},
            {"timestamp": at(9, 2), "type": "response_item", "payload": {
                "type": "function_call", "name": "shell", "arguments": json.dumps({"command": ["git", "commit", "-m", "x"]})}},
            {"timestamp": at(9, 4), "type": "event_msg", "payload": {
                "type": "token_count", "info": {"total_token_usage": {"output_tokens": 500}}}},
        ]
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "rollout-1.jsonl"
            p.write_text("\n".join(json.dumps(r) for r in recs))
            s = al.parse_codex_session(p, PERTH)
        r = s.row(DAY)
        self.assertEqual(r["human_prompts"], 1)
        self.assertAlmostEqual(r["agent_active_min"], 4.0)
        self.assertEqual(r["commits"], 1)
        self.assertEqual(r["output_tokens"], 500)
        self.assertEqual(r["project"], "/repo")


if __name__ == "__main__":
    unittest.main()
