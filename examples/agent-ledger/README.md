# agent-ledger

A daily ledger of how much time you spend with agents, how much time they spend working, and what they produce.

It reads the session logs your agents already write on your Mac. There is nothing new to log by hand, no API keys and no network calls. It needs only the `python3` that ships with macOS.

```
python3 agent_ledger.py collect --print     # read the logs, update the ledger, show the last 7 days
python3 agent_ledger.py report --days 30    # show the ledger without re-reading logs
./install-launchd.sh                        # run collect every night at 11:50 pm
```

The ledger is written to `~/AF_OS/metrics/agent-ledger/` by default. Set `--out` or `AGENT_LEDGER_OUT` to put it somewhere else.

| File | What it holds |
| --- | --- |
| `daily.csv` | One row per day: your time, agent time, outputs, cost, and the ratios between them |
| `sessions.csv` | One row per session per day, for drilling into where the time went |
| `report.md` | The last 7 days as tables, readable in any Markdown viewer or by an agent |

## What it measures

Everything is split by local day (Perth time by default; change it with `--tz`).

### Your time: `human_min`

This is an estimate of engaged attention, not a stopwatch. No log records when you are reading. What the logs do record is when an agent finished and when you next replied.

- If you replied within **15 minutes** of an agent finishing, the whole gap counts. You were most likely reading and deciding.
- If you replied after a longer gap, only **2 minutes** count. You were away, then came back, read the reply and typed.

When several sessions run at once, overlapping time is counted once. Ten minutes spent watching three agents is ten minutes of your day, not thirty.

The estimate leaves out work you do outside the agents: reading a PR on GitHub, testing on your phone, thinking in the shower. Treat it as a lower bound.

### Agent time: `agent_min` and `agent_wall_min`

A turn runs from a prompt to the agent's last action before the next prompt. Agent time is the time inside turns where something was happening.

- **`agent_min`** adds up every session. Three agents working for ten minutes is 30 agent-minutes. This is the workforce number.
- **`agent_wall_min`** is the clock time when at least one agent was working. The same example gives 10 minutes.
- **`agent_wait_min`** is a silence of **10 minutes or more** inside a turn. It usually means a permission prompt waiting for you, or a very long command. It is kept out of agent time so it doesn't inflate it.
- **`subagent_min`** is time spent by sub-agents a session started.

A single command that genuinely runs for more than 10 minutes, such as a long build, will be counted as waiting. If that is common for you, raise the limit with `--idle-cap 20`.

### Interaction between agents

- **`agent_prompts`** counts turns started by something other than you: a sub-agent reporting back, a scheduled task, a webhook, or another session messaging this one.
- **`agent_handoffs`** counts work handed to another agent: sub-agents, workflows, new sessions, messages to other sessions, and suggested tasks.
- **`subagent_min`** (above) is how long those sub-agents worked.

Read together, these show how much work agents pass to each other rather than back to you.

### Work produced: the output columns

These count concrete actions an agent took. A call that failed is not counted.

| Column | Counts |
| --- | --- |
| `files_edited` | Distinct files changed |
| `commits`, `pushes` | `git commit` and `git push`, and GitHub API file commits |
| `prs_opened`, `prs_merged` | Pull requests opened or merged |
| `docs_written` | Notion pages created or updated (each page counts), artifacts published, Google Docs, Sheets and Slides written, beehiiv posts saved |
| `messages_drafted`, `messages_sent` | Gmail drafts, and emails sent, replied to or forwarded |
| `posts_queued` | Social posts queued in Metricool |
| `calendar_events` | Events created or updated |
| `research_calls` | Web searches and page fetches. Not counted in `outputs_total`, because research is an input, not an output |
| `agent_handoffs` | See above |
| `lines_added`, `lines_removed` | Code lines changed, as Claude Code itself reports them |

`outputs_total` sums the output columns, leaving out research and raw tool calls.

### The ratios

- **`agent_min_per_human_hour`**: agent-minutes of work per hour of your attention. This is your leverage.
- **`outputs_per_human_hour`**: concrete outputs per hour of your attention.

Volume is not value. Eight Notion pages created count as 8, and one merged fix to a live bug counts as 1. Use the ratios to spot trends and compare like with like (this week against last week, or one kind of work against another), not to judge a single day. For outcomes, the Hub task moved to Done is still the record.

### Cost and tokens

`cost_usd` and the token columns come from Claude Code's own running totals in the log. They match what Claude Code shows you with `/cost`.

## What it can see, and what it can't

| Source | Covered |
| --- | --- |
| Claude Code on your Mac, including sessions you drive from the phone or desktop app through Remote Control | Yes |
| Codex on your Mac (`~/.codex/sessions`) | Experimental. Written from the published log format but not yet checked against your own logs. Turn it off with `--no-codex` |
| Claude Code cloud sessions (claude.ai/code) | No. Their logs live in the cloud container, not on your Mac |
| Scheduled tasks | No, same reason |
| Cowork, claude.ai chats, ChatGPT, Notion agents | No. They don't write local logs |

Claude Code deletes local session logs after 30 days by default. Run `collect` at least monthly (the nightly job takes care of this) and the ledger keeps the history after the logs are gone. A day whose logs have only partly been cleaned up keeps the totals it was given while all its logs were there, rather than being recomputed from the logs that are left. Days rebuilt from saved rows, instead of from the logs themselves, sum your time across sessions rather than merging overlaps. That makes them an upper bound.

## Install the nightly run

```
cd examples/agent-ledger
./install-launchd.sh            # runs collect at 11:50 pm every night
./install-launchd.sh --remove   # stops it
```

The job writes its own log to `~/Library/Logs/agent-ledger.log`.

macOS may block a background job from writing into iCloud Drive (where `~/AF_OS` lives). If the log shows `Operation not permitted`, either give `/usr/bin/python3` Full Disk Access in System Settings, Privacy & Security, or point the ledger at a local folder:

```
AGENT_LEDGER_OUT=~/agent-ledger ./install-launchd.sh
```

## Tests

```
python3 -m unittest discover -s tests
```

The tests cover each rule above: prompt sources, engaged gaps, waiting time, midnight splits, not counting parallel time twice, failed calls, per-page counting, cost deltas, history kept after logs are deleted, and the Codex reader.
