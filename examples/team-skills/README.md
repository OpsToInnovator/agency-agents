# Team Skills — shared skill management for teams

Every AI coding tool now reads *skills*: a `SKILL.md` file with `name` and
`description` front matter and Markdown instructions, one per directory, in
`~/.claude/skills/`, `~/.gemini/config/skills/`, `~/.codex/skills/`,
`~/.osaurus/skills/` and so on. That is exactly the format this repository's
`scripts/convert.sh` renders agents into. It works well for one person. It
falls apart for a team: everyone keeps a private copy, nobody knows which
version a teammate is running, an edit that helps one person silently
regresses another, and there is no record of who approved what.

Team Skills gives a team **one reviewed catalog** and keeps every member's
tools in sync with it:

- **Drafts, reviews, immutable versions.** A contributor drafts a skill, submits
  it with a semantic-version bump, and a *different* maintainer approves it.
  Approval publishes an immutable version stamped with its number and SHA-256
  digest. Rejections carry a reason; drafts are frozen while in review.
- **Roles.** `viewer` (read and install), `contributor` (draft and submit their
  own skills), `maintainer` (edit anything, approve, reject, deprecate),
  `owner` (also manage members and tokens).
- **Install and sync.** Members install published versions into any supported
  tool directory. `status` tells each member which copies are current,
  outdated, locally modified, missing or deprecated, and `sync` updates the
  ones that are safe to update. Installs are recorded, so the team dashboard
  shows adoption.
- **Digests and a catalog index.** Every version has a content hash; `index`
  publishes the set of current versions with their digests and a hash of the
  whole index, so any runtime can prove it retrieved the same generation as
  another.
- **Three interfaces, one engine.** A CLI, an HTTP API and a browser UI all
  call the same service, so the rules are identical everywhere. The CLI works
  against a local SQLite file or against a running server.
- **Import.** Pull existing `SKILL.md` folders, or this repository's agent
  files, into the catalog as drafts.

Python 3.11+, standard library only. No accounts, no cloud: one SQLite file
per team, hosted by whichever machine runs `teamskills serve`.

## Quick start (one machine)

```bash
cd examples/team-skills
export PYTHONPATH=$PWD            # or: pip install -e .
export TEAMSKILLS_DB=./acme.sqlite

python -m teamskills init --team acme --name "Acme Platform" --owner ana
export TEAMSKILLS_TEAM=acme TEAMSKILLS_USER=ana

python -m teamskills members add ben --role maintainer
python -m teamskills members add cai --role contributor

# cai drafts a skill from the template, or from a file / directory
python -m teamskills --as cai skills new --name release-notes \
    --description "Write release notes from merged PRs in the team's house style."
python -m teamskills --as cai skills edit release-notes ./release-notes/SKILL.md
python -m teamskills --as cai skills diff release-notes
python -m teamskills --as cai skills submit release-notes --note "first cut"

# ben reviews and publishes 1.0.0
python -m teamskills reviews
python -m teamskills --as ben skills approve release-notes --comment "LGTM"

# everyone installs it into their tools
python -m teamskills --as cai install release-notes                       # ~/.claude/skills/release-notes/SKILL.md
python -m teamskills --as cai install release-notes --target codex
python -m teamskills --as cai install release-notes --target claude-code-project --project ~/src/app
python -m teamskills --as cai status                                      # exit code 2 if anything needs attention
python -m teamskills --as cai sync                                        # update outdated / missing copies
```

`teamskills --help` lists every command; each subcommand has its own `--help`.
Add `--json` to any command for machine-readable output.

## Running it for a team

Start the server on a machine the team can reach and hand each member the
token printed when they were added:

```bash
python -m teamskills --db /srv/teamskills/acme.sqlite serve --host 0.0.0.0 --port 8765
```

Members then either open `http://host:8765/` in a browser (dashboard, skill
editor, diffs, reviews, members, activity) or point the CLI at the server:

```bash
export TEAMSKILLS_SERVER=http://host:8765 TEAMSKILLS_TOKEN=ts_...
python -m teamskills whoami
python -m teamskills skills list --status published
python -m teamskills install release-notes && python -m teamskills sync
```

The server speaks plain HTTP. Put it behind TLS (a reverse proxy, Tailscale,
an SSH tunnel) before exposing it beyond a trusted network; tokens are bearer
credentials. For a demo on a trusted network, `serve --no-auth` accepts
`X-TeamSkills-Team` and `X-TeamSkills-User` headers instead of tokens.

## The workflow

```
            create / edit           submit               approve (different maintainer)
   (none) ───────────────▶ draft ───────────▶ in_review ─────────────────────────────▶ published vN
                            ▲                    │                                        │  ▲
                            │   reject / withdraw │                          edit (new draft on top)
                            └────────────────────┘                                        │  │
                                                                       deprecate ◀────────┘  └──── restore
```

- A skill's slug is its front matter `name` and never changes.
- The first approval publishes `1.0.0`; later submissions choose `major`,
  `minor` or `patch`.
- The published content is the draft re-rendered with `version:` stamped
  into the front matter; unknown front matter keys (`license`,
  `allowed-tools`, `metadata`, ...) are preserved.
- A published skill can carry a new draft on top of it. Discarding the draft
  restores the published metadata. A never-published skill is deleted, not
  deprecated; a published one is deprecated, never deleted, so installed
  copies keep resolving.
- Every action is written to the activity log with its actor.

## Skill file format

```markdown
---
name: release-notes                  # required; lowercase words joined by hyphens, max 64 chars
description: Write release notes ... # required; one sentence, max 1024 chars
tags: [docs, release]                # optional
version: 1.0.0                       # set by Team Skills on publish
license: MIT                         # any other keys are kept as-is
---

# Release Notes

## When to use this skill
...
## Steps
...
```

`teamskills skills validate path/to/SKILL.md` checks a file offline and
reports problems (fatal) and warnings (advice). The same checks run on every
create, edit and submit.

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

`TEAMSKILLS_HOME` overrides the home directory used for global targets.
`status` compares each recorded install with the file on disk and with the
catalog:

| State | Meaning | `sync` does |
|---|---|---|
| `current` | installed hash matches, latest version | nothing |
| `outdated` | a newer version is published | reinstalls |
| `missing` | the file was deleted | reinstalls |
| `modified` | the file differs from what was installed | skips unless `--force` |
| `deprecated` | the skill was deprecated | nothing; remove it with `uninstall` |

## Importing what you already have

```bash
# this repository's agents, one division at a time (34 engineering agents become drafts)
python -m teamskills import ../../engineering

# a directory of <name>/SKILL.md folders, or a single file
python -m teamskills import ~/.claude/skills
python -m teamskills import ./my-skill/SKILL.md --update   # replace an existing draft
```

Agent front matter (`name: Backend Architect`, `color`, `emoji`, `vibe`,
`services`) is converted the way `scripts/convert.sh` does it: the name is
slugified and the agent-only keys are dropped. Imported skills arrive as
drafts and go through review like anything else.

## HTTP API

One endpoint. Every operation the CLI has is available by name:

```
POST /api/rpc
Authorization: Bearer ts_...
{"op": "list_skills", "args": {"status": "published"}}

→ {"ok": true, "result": [...]}
→ {"ok": false, "error": {"code": "forbidden", "message": "..."}}   (HTTP 400/401/403/404/409)
```

`GET /api/health` returns the version, the auth mode and the list of
operations. Operations take the same arguments as the corresponding
`Service` method minus `team` and `actor`, which come from the token.

## Governance mapping

If you are running a skill library with stricter release gates (bounded
test authority, production approval as a separate step, verifier receipts),
Team Skills covers the mechanical part and leaves the policy to you:

| Need | Here |
|---|---|
| Canonical ID | the skill slug (`name`), immutable |
| Exact version and digest | `versions` / `skills cat --ref 1.2.0`; every version carries its SHA-256 |
| Index generation | `index`: current versions, digests, and a hash of the whole index |
| Retrieval receipt | `status --json`: recorded install hash and time versus the published version and its time |
| Separation of author and approver | enforced: an approver must differ from the submitter |
| "In review" never implies approval | a skill in review keeps serving its last *published* version; installs resolve `latest`, never the draft |
| Production approval as a further gate | model it as a tag or a deprecation policy, or keep two teams (`pilot`, `prod`) and promote by importing the exact published file |

## Layout

```
teamskills/
  frontmatter.py   minimal YAML-subset front matter parser/writer (no PyYAML)
  skillfile.py     SKILL.md parsing, validation, version stamping, hashing
  semver.py        MAJOR.MINOR.PATCH helpers
  store.py         SQLite schema and queries
  service.py       roles, workflow, versions, installs, activity, index (@rpc methods)
  session.py       LocalSession (in-process) and RemoteSession (HTTP), same call() surface
  installer.py     targets, install/uninstall/status/sync with drift detection
  importer.py      import SKILL.md folders and Agency agent files
  server.py        ThreadingHTTPServer: POST /api/rpc, GET /api/health, GET /
  cli.py           argparse CLI
  web/index.html   single-file browser UI (vanilla JS)
tests/             pytest: parser, service rules, installer, importer, server, CLI
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
- `sync` only touches installs it recorded. Skills copied into a tool
  directory by hand are invisible to it.
- The web UI uses browser prompts for review notes and reasons; it is a
  working console, not a design system.
