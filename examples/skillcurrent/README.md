# SkillCurrent — better methods, not more files

Every AI coding tool now reads *skills*: a `SKILL.md` with `name` and
`description` front matter and Markdown instructions, one per directory, in
`~/.claude/skills/`, `~/.gemini/config/skills/`, `~/.codex/skills/`,
`~/.osaurus/skills/` and so on. It is exactly the format this repository's
`scripts/convert.sh` renders agents into. It works for one person. For a team
it falls apart: everyone keeps a private copy, nobody knows which version a
teammate is running, an edit that helps one person silently regresses
another, and there is no record of who approved what.

SkillCurrent gives a team **one reviewed catalog** and walks every change
through four steps:

| Step | What happens | What it produces |
|---|---|---|
| **01 Edit** | A contributor improves a draft. Production keeps serving the last release. | a draft with a diff against production |
| **02 Test** | Deterministic local checks run against the exact draft bytes: built-in package, safety and structure checks plus the team's own rules. | a recorded check run; submission is blocked until it passes |
| **03 Release** | A *different* maintainer approves the exact bytes, which freezes an immutable version with a SHA-256 digest. A channel (`canary`, then `production`) is pointed at it. Rollback points the channel back. | approved versions, channel history, a catalog index |
| **04 Adoption** | Every environment (member + host + runtime) records receipts: installed, verified, loaded, task-tested. The team sees where the release actually landed. | an adoption view with "needs attention" |

Three interfaces share one engine: a CLI (against a local SQLite file or a
server), a JSON-RPC style HTTP API, and a browser UI built around the four
steps. Python 3.11+, standard library only.

## Quick start (one machine)

```bash
cd examples/skillcurrent
export PYTHONPATH=$PWD            # or: pip install -e .
export SKILLCURRENT_DB=./northstar.sqlite

python -m skillcurrent init --team northstar --name "Northstar Studio" --owner maya
export SKILLCURRENT_TEAM=northstar SKILLCURRENT_USER=maya
python -m skillcurrent members add noah --role maintainer
python -m skillcurrent members add sam  --role contributor

# 01 Edit: sam drafts a skill (from a file, a directory, or the template)
python -m skillcurrent --as sam skills new ./refund-handoff/SKILL.md
python -m skillcurrent --as sam skills diff refund-handoff

# 02 Test: checks must pass on the exact draft before it can be submitted
python -m skillcurrent --as sam skills checks refund-handoff
python -m skillcurrent --as sam skills submit refund-handoff --note "baseline"

# 03 Release: noah approves the bytes, then releases to canary and production
python -m skillcurrent --as noah skills approve refund-handoff
python -m skillcurrent --as noah skills release refund-handoff --channel canary
python -m skillcurrent --as noah skills release refund-handoff            # production
python -m skillcurrent --as noah skills rollback refund-handoff --reason "handoff regression"

# 04 Adoption: members install and keep in sync; every step leaves a receipt
python -m skillcurrent --as sam install refund-handoff                    # ~/.claude/skills/refund-handoff/SKILL.md
python -m skillcurrent --as sam install refund-handoff --target codex --channel canary
python -m skillcurrent --as sam status                                    # re-hashes copies, sends 'verified'
python -m skillcurrent --as sam sync                                      # outdated / missing copies follow their channel
python -m skillcurrent --as sam report refund-handoff loaded              # from a tool hook
python -m skillcurrent adoption --slug refund-handoff
python -m skillcurrent skills passport refund-handoff
```

`skillcurrent --help` lists every command; each has its own `--help`. Add
`--json` (before the command) for machine-readable output.

## Running it for a team

```bash
python -m skillcurrent --db /srv/skillcurrent/northstar.sqlite serve --host 0.0.0.0 --port 8765
# or
docker build -t skillcurrent . && docker run -p 8765:8765 -v skillcurrent-data:/data skillcurrent
```

Members open `http://host:8765/` in a browser (skill library, the four-step
change room, reviews, adoption, check rules, members, activity) or point the
CLI at the server with the token printed when they were added:

```bash
export SKILLCURRENT_SERVER=http://host:8765 SKILLCURRENT_TOKEN=ts_...
python -m skillcurrent whoami
python -m skillcurrent install refund-handoff && python -m skillcurrent sync
```

The server speaks plain HTTP. Put it behind TLS (a reverse proxy, Tailscale,
an SSH tunnel) before exposing it beyond a trusted network; tokens are bearer
credentials. For a demo on a trusted network, `serve --no-auth` accepts
`X-SkillCurrent-Team` and `X-SkillCurrent-User` headers instead of tokens.

## The rules the engine enforces

- **Roles.** `viewer` reads and installs; `contributor` drafts and submits
  their own skills; `maintainer` edits anything, approves, releases, rolls
  back, deprecates and manages check rules; `owner` also manages members.
- **A skill's slug is its front matter `name` and never changes.** The
  canonical id is `team/slug`.
- **Checks gate submission.** `submit` needs a recorded, passing check run
  whose hash equals the draft's. Edit the draft and the run is stale.
- **Approval attaches to bytes.** The approver must differ from the
  submitter. Approval stamps `version:` into the front matter and stores the
  SHA-256 of the result. Approved is not released: nothing is installed
  until a channel points at the version (`approve --release production`
  collapses the two steps for teams that do not stage rollouts).
- **Channels resolve installs.** An environment follows `production`
  (default) or `canary`. `status` compares the installed bytes with the
  file on disk and with what its channel serves; `sync` reinstalls
  `outdated` and `missing` copies and leaves `modified` ones alone unless
  `--force`. Rollback moves the channel, never files.
- **Receipts are evidence about one event.** `installed` (bytes written),
  `verified` (re-hashed and matched), `loaded` (a runtime hook reported
  loading it), `task_tested` (a named fixture passed). None of them proves
  an agent always follows the skill, and the UI says so.
- **Published skills are deprecated, never deleted**, so installed copies
  keep resolving. Unapproved drafts can be deleted.
- Every action goes to the activity log with its actor.

## Checks

Built-in checks, each a transparent text rule with a one-line explanation:

| Check | Category | Looks for |
|---|---|---|
| Valid skill package | package | parses as a SKILL.md with a valid name and description |
| Description says when to use the skill | selection | 20 to 1024 characters, not placeholder text |
| Required skill sections | package | a "when to use" heading and a steps/method heading |
| No template placeholders left | package | none of the template phrases, no TODO/TBD |
| No embedded secrets | safety | common credential shapes (AWS, GitHub, Slack, API keys, private keys, password assignments) |
| No machine-specific paths | package | no `/Users/<name>`, `/home/<name>`, `C:\Users\<name>` |
| Method is written as steps | evidence | a numbered list or at least two bullets |

Team rules are stored in the catalog and run on top:

```bash
python -m skillcurrent rules add inspect-before-interrupt require "inspect .* before asking" \
    --category evidence --rationale "Inspect what the agent can already see before interrupting the user."
python -m skillcurrent rules add no-screenshot-first forbid "ask (the user )?for a screenshot"
python -m skillcurrent rules add guardrails-section section "guardrails|boundar"
```

`skills validate path/to/SKILL.md` runs the parser and the built-in checks
offline, without a team.

## Skill file format

```markdown
---
name: refund-handoff                  # required; lowercase words joined by hyphens, max 64 chars
description: Prepare an evidence-... # required; one sentence, max 1024 chars
tags: [client-service, handoff]      # optional
version: 1.1.0                       # set by SkillCurrent on approval
license: MIT                         # any other keys are kept as-is
---

# Refund handoff

## When to use
...
## Method
1. ...
```

## Install targets

| Target | Directory |
|---|---|
| `claude-code` (default) | `~/.claude/skills/<name>/SKILL.md` |
| `claude-code-project` | `<project>/.claude/skills/<name>/SKILL.md` |
| `antigravity` | `~/.gemini/config/skills/<name>/SKILL.md` |
| `antigravity-project` | `<project>/.agents/skills/<name>/SKILL.md` |
| `codex` | `~/.codex/skills/<name>/SKILL.md` |
| `osaurus` | `~/.osaurus/skills/<name>/SKILL.md` |
| `custom` | `--dir <directory>/<name>/SKILL.md` |

`SKILLCURRENT_HOME` overrides the home directory for global targets;
`SKILLCURRENT_HOST` (or `--host`) names the machine in receipts (default:
the hostname).

| `status` state | Meaning | `sync` does |
|---|---|---|
| `current` | bytes intact, same version the channel serves | nothing (sends `verified`) |
| `outdated` | the channel moved (release or rollback) | reinstalls |
| `missing` | the file was deleted | reinstalls |
| `modified` | the file differs from what was installed | skips unless `--force` |
| `deprecated` | the skill was deprecated | nothing; `uninstall` it |
| `unreleased` | the channel has nothing (installed from `--ref`) | nothing |

## Importing what you already have

```bash
python -m skillcurrent import ../../engineering      # this repo's agents become drafts
python -m skillcurrent import ~/.claude/skills       # <name>/SKILL.md folders
python -m skillcurrent import ./my-skill/SKILL.md --update
```

Agent front matter (`name: Backend Architect`, `color`, `emoji`, `vibe`,
`services`) is converted the way `scripts/convert.sh` does it.

## HTTP API

```
POST /api/rpc
Authorization: Bearer ts_...
{"op": "adoption", "args": {"slug": "refund-handoff"}}

→ {"ok": true, "result": {...}}
→ {"ok": false, "error": {"code": "forbidden", "message": "..."}}   (HTTP 400/401/403/404/409)
```

`GET /api/health` returns the version, the auth mode and the operation
list. Operations take the same arguments as the corresponding `Service`
method minus `team` and `actor`, which come from the token. Notable ones:
`run_checks`, `submit_review`, `approve`, `release`, `rollback`,
`release_history`, `passport`, `record_install`, `report`, `adoption`,
`catalog_index`.

`catalog_index` lists every channel's current version with its digest and a
hash of the whole index, so two runtimes can prove they retrieved the same
generation.

## Beta landing page and waitlist

`landing/index.html` is a single-file beta landing page (inline CSS, light
and dark mode, no build step). `skillcurrent serve` serves it at `/beta`,
and its form posts to `POST /api/beta` on the same server, which stores
sign-ups (email, team size, tools in use, note) in the catalog database.
Owners read them with `skillcurrent beta` or the `list_beta_signups`
operation. The page also works from any static host; when no endpoint
answers, the form shows the visitor their entry to send by hand.

## Layout

```
skillcurrent/
  frontmatter.py   minimal YAML-subset front matter parser/writer (no PyYAML)
  skillfile.py     SKILL.md parsing, validation, version stamping, hashing
  checks.py        built-in checks and team rules
  semver.py        MAJOR.MINOR.PATCH helpers
  store.py         SQLite schema and queries
  service.py       roles, workflow, channels, checks, receipts, adoption (@rpc methods)
  session.py       LocalSession (in-process) and RemoteSession (HTTP), same call() surface
  installer.py     targets, install/uninstall/status/sync/report with drift detection
  importer.py      import SKILL.md folders and Agency agent files
  server.py        ThreadingHTTPServer: POST /api/rpc, GET /api/health, GET /
  cli.py           argparse CLI
  web/index.html   single-file browser UI (vanilla JS): library, change room, adoption, rules
landing/index.html beta landing page, served at /beta; form posts to /api/beta
tests/             pytest: parser, checks, service rules, releases, installer, importer, server, CLI
```

## Tests

```bash
pip install pytest    # the app itself has no dependencies
pytest
```

## Limits, honestly

- One server process per database file. SQLite with a process-level lock is
  plenty for a team, not for thousands of concurrent writers.
- Tokens are the whole authentication story: no SSO, no expiry, rotate with
  `members rotate-token`.
- `loaded` and `task_tested` receipts come from whatever hook or test
  runner calls `skillcurrent report`; nothing here observes a runtime on its
  own.
- `sync` only touches installs it recorded. Skills copied into a tool
  directory by hand are invisible to it.
- The web UI uses browser prompts for review notes and reasons.
