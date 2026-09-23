# SkillCurrent — better methods, not more files

Every AI coding tool now reads *skills*: a `SKILL.md` with `name` and
`description` front matter and Markdown instructions, one per directory, in
`~/.claude/skills/`, `~/.agents/skills/`, `~/.gemini/config/skills/`,
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

## Install

Python 3.11 or newer, no other dependencies. Linux and macOS are tested in
CI; Windows is not tested yet.

```bash
# from GitHub, into its own environment (pipx keeps it off your system Python)
pipx install "git+https://github.com/OpsToInnovator/agency-agents@main#subdirectory=examples/skillcurrent"
# or with pip, inside a virtual environment
pip install "git+https://github.com/OpsToInnovator/agency-agents@main#subdirectory=examples/skillcurrent"
# or from a checkout
cd examples/skillcurrent && pip install -e .
```

Every example below also works as `python -m skillcurrent` from
`examples/skillcurrent` with `PYTHONPATH=$PWD`.

## Quick start (one machine)

```bash
cd examples/skillcurrent
export PYTHONPATH=$PWD            # or install it as above
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
credentials. For a demo on your own machine, `serve --no-auth` accepts
`X-SkillCurrent-Team` and `X-SkillCurrent-User` headers instead of tokens; it
refuses to start on anything but a loopback address.

`serve` options worth knowing:

| Flag | What it does |
|---|---|
| `--public` | internet-facing waitlist host: serves only the beta page, its assets, `POST /api/beta` and the waitlist operations; the web UI is not served |
| `--trust-proxy` | read the client address from `X-Forwarded-For` (only behind your own reverse proxy) for the sign-up rate limit |
| `--allow-origin URL` | allow the beta form on another origin (a static copy of the page) to post here; repeatable |
| `--contact EMAIL` | the reply address shown on the beta page [`SKILLCURRENT_CONTACT`] |
| `--site-url URL` | the page's public address, for link previews [`SKILLCURRENT_SITE_URL`] |
| `--waitlist-team SLUG` | the team whose owners may read the waitlist [`SKILLCURRENT_WAITLIST_TEAM`] |
| `--access-log` / `--quiet` | request log with client addresses: on by default for a team server, off with `--public` unless `--access-log`; `--quiet` turns it off |

`skillcurrent backup DEST` writes a consistent copy of the database while
the server runs. `deploy/` has a Caddyfile (automatic HTTPS), a Docker
Compose file, a systemd unit and a daily backup script.

## The rules the engine enforces

- **Roles.** `viewer` reads and installs; `contributor` drafts and submits
  their own skills; `maintainer` edits anything, approves, releases, rolls
  back, deprecates and manages check rules; `owner` also manages members.
- **A skill's slug is its front matter `name` and never changes.** The
  canonical id is `team/slug`.
- **Checks gate submission.** `submit` needs a recorded, passing check run
  whose hash equals the draft's. Edit the draft and the run is stale.
- **Approval attaches to bytes.** The approver must differ from the
  submitter and from anyone who created or edited the draft since the last
  approval (checked per account). Approval stamps `version:` into the front matter and stores the
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
| `codex` | `~/.agents/skills/<name>/SKILL.md` |
| `osaurus` | `~/.osaurus/skills/<name>/SKILL.md` |
| `custom` | `--dir <directory>/<name>/SKILL.md` |

The folders were checked against each vendor's documentation on 22
September 2026. Codex documents `~/.agents/skills` for user skills; its
older `~/.codex/skills` is deprecated. Gemini CLI and Cursor say they read
`~/.agents/skills` too, and Cursor also reads `~/.claude/skills`.
Antigravity documents `~/.gemini/config/skills` and a workspace's
`.agents/skills`. The Osaurus folder has not been checked. Vendors move
these folders; if a tool doesn't pick a skill up, install it with
`--target custom --dir <folder>`.

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

## Beta page, waitlist and beta report

`skillcurrent/web/beta.html` is the beta page (inline CSS, light and dark
mode, self-hosted fonts, no third-party requests). It ships inside the
package, so every install can serve or build it:

- `skillcurrent serve` serves it at `/beta`; with `--public` it is also the
  front page. Its form posts to `POST /api/beta` on the same server, which
  stores sign-ups (email, team size, tools, note, and the link's `c` and
  `utm_` tags) in the database. A repeat sign-up for the same email never
  overwrites: it fills gaps, unions tools, appends the note and counts.
  Sign-ups are rate limited per client address, held in memory only.
- `skillcurrent build-landing OUT --site-url URL [--endpoint ...]` writes
  the page, its assets, the font licence and a `_headers` file for a static
  host. `--endpoint` is `/api/beta` (the default), the https URL of a
  `serve --public` host (also pass `--allow-origin` there), `netlify`
  (Netlify Forms) or `mailto` (no server: the visitor's email app sends the
  entry to `--contact`).

The privacy line under the form is generated to match how the form
submits and whether the server keeps a request log.

Owners of the waitlist team manage sign-ups:

```bash
skillcurrent beta                      # list
skillcurrent beta --sources            # sign-ups per link tag (?c=drift, utm_source=...)
skillcurrent beta remove person@x.co   # a deletion request
skillcurrent beta import export.csv    # merge a static host's form export
```

Beta teams answer two questions with `skillcurrent beta-report [--days 7]`:
did the team keep review switched on, and did sync ever catch a bad copy
(a hand-modified or missing copy). It holds counts only, no skill content,
names or emails, and it says "never checked" when no status or sync run was
reported, so a silent hook is not mistaken for a clean one. `BETA.md` is the
guide for pilot teams; `LAUNCH.md` is the plan for running the beta.

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
  server.py        ThreadingHTTPServer: /api/rpc, /api/beta, /api/health, the UI and the beta page; --public mode
  cli.py           argparse CLI
  web/index.html   single-file browser UI (vanilla JS): library, change room, adoption, rules
  web/beta.html    the beta page, served at /beta (and / with --public), or built with build-landing
  web/assets/      og.png and self-hosted fonts (SIL OFL, licence in fonts/OFL.txt)
deploy/            Caddyfile, docker-compose.yml, systemd unit, backup script
docs/beta/         beta terms, call scripts, email templates
docs/business/     brief for legal review
marketing/ads/     ad creatives (rendered from HTML) and channel copy
tests/             pytest: parser, checks, service rules, releases, installer, importer, server, CLI,
                   public mode, beta measurement; tests/browser/ drives the beta form in Chromium
BETA.md            guide for pilot teams          LAUNCH.md   plan for running the beta
SECURITY.md        reporting a vulnerability       CONTRIBUTING.md
BUSINESS.md        business model: offer, pricing hypotheses, validation, unit economics
```

## Tests

```bash
pip install pytest    # the app itself has no dependencies
pytest
```

One test drives the landing page's waitlist form in a real browser
(`tests/browser/landing_form.mjs`, Node + Playwright). It runs when `node`
and a `playwright` package are available and is skipped otherwise, so the
suite still passes on a machine with only Python. Another runs the page's
terminal walkthrough command by command, so the page cannot drift from the
CLI. CI (`.github/workflows/skillcurrent-tests.yml`) runs the suite on
Linux and macOS, the browser test, and a clean install with a `--public`
smoke test.

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
- The sign-up rate limit lives in one process's memory; it resets on
  restart and is not shared between processes.
- Schema upgrades are additive only (new columns and tables), so an upgrade
  keeps the evidence a team has collected. Take a `backup` first anyway.

## Licence

MIT, copyright ApexForm Life Pty Ltd (Perth, Western Australia). See
[`LICENSE`](LICENSE).
