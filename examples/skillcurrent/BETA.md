# SkillCurrent beta: guide for pilot teams

Thanks for trying SkillCurrent. This guide covers setup, the one hook that
makes the beta measurable, the weekly report, upgrades and leaving.

## What the beta is

- **What you get.** The tool, free during the beta, and direct help from us
  while it is rough: an install session, and replies on your email thread
  within two working days.
- **What we watch.** Two things only: whether your team keeps review
  switched on, and whether `sync` ever catches a bad copy of a skill. A bad
  copy is one that was hand-modified or deleted.
- **What you send.** Once a week, the output of
  `skillcurrent --json beta-report --days 7`, by email. It holds counts only:
  no skill content, no member names, no emails. Read it before you send it.
- **What stays with you.** Everything else. The catalog database, your skills
  and your tokens live on your machines. We never get access to them.
- **How long.** Four to six weeks. You can stop at any time, for any reason.
  The terms are in [docs/beta/terms.md](docs/beta/terms.md).

## Before the install session (about 15 minutes)

1. **Python 3.11 or newer** on every member's machine and on the server.
   On macOS use python.org or Homebrew; on Linux, your distribution's
   package. Install [pipx](https://pipx.pypa.io/) to keep SkillCurrent off
   your system Python.
2. **A place for the catalog server** that every member can reach: a small
   VM, an office machine or a Tailscale node. It speaks plain HTTP, so put
   TLS in front of it (Caddy, a tunnel, your VPN) if it is reachable beyond a
   trusted network. The tokens it issues are bearer credentials.
3. **Two maintainers at least.** Approval must come from someone other than
   the author, so one maintainer alone cannot release anything.
4. **A backup of your current skills** on each machine. `install` writes the
   approved version over the file at the same path:

   ```bash
   cp -a ~/.claude/skills ~/.claude/skills.before-skillcurrent 2>/dev/null
   ```

Supported: macOS and Linux, tested in CI. Windows is not tested yet; tell us
before the session if you need it.

## The install session (about 60 minutes, together)

### 1. The server (one person)

```bash
pipx install "git+https://github.com/OpsToInnovator/agency-agents@main#subdirectory=examples/skillcurrent"
export SKILLCURRENT_DB=/srv/skillcurrent/catalog.sqlite
skillcurrent init --team <team> --owner <your-handle>     # prints your token once
skillcurrent --team <team> --as <your-handle> members add <handle> --role maintainer
skillcurrent --team <team> --as <your-handle> members add <handle> --role contributor
skillcurrent serve --host 0.0.0.0 --port 8765             # behind TLS, see above
```

Each `members add` prints that member's token once. Send it privately.
`deploy/` has a systemd unit, a Docker Compose file and a Caddyfile you can
adapt. For a team server, drop `--public`, `--contact` and `--site-url`
from them.

### 2. Each member

```bash
pipx install "git+https://github.com/OpsToInnovator/agency-agents@main#subdirectory=examples/skillcurrent"
mkdir -p ~/.skillcurrent && chmod 700 ~/.skillcurrent
cat > ~/.skillcurrent/env <<'EOF'
export SKILLCURRENT_SERVER=https://skills.your-team.example
export SKILLCURRENT_TOKEN=ts_paste_your_token_here
EOF
echo "SKILLCURRENT_BIN=$(command -v skillcurrent)" >> ~/.skillcurrent/env
chmod 600 ~/.skillcurrent/env
. ~/.skillcurrent/env && skillcurrent whoami
```

### 3. Bring in the skills you already have

```bash
skillcurrent import ~/.claude/skills        # each <name>/SKILL.md becomes a draft
skillcurrent skills checks <slug>           # fix what the checks flag
skillcurrent skills submit <slug> --note "baseline"
# a different maintainer:
skillcurrent skills approve <slug> --release production
```

Then every member installs what the team released:

```bash
skillcurrent install <slug>                             # Claude Code, ~/.claude/skills
skillcurrent install <slug> --target codex              # also: antigravity, osaurus, *-project
skillcurrent install <slug> --target custom --dir <folder>
skillcurrent status
```

`skillcurrent targets` lists every target and its folder. The `codex` target
writes to `~/.agents/skills`, which Codex, Gemini CLI and Cursor all
document reading. Cursor also reads `~/.claude/skills`, so the default
target covers it. These folders were checked against the vendors' docs in
September 2026; the Osaurus folder was not. If your version of a tool reads
skills from somewhere else, use `--target custom --dir <folder>`, and tell
us so we can fix the target.

### 4. The hook: run sync without anyone remembering

The second beta question depends on `sync` running regularly. Each member
adds one small script that runs `sync` and writes the result to a log file.

Save this as `~/.skillcurrent/hook.sh` and make it executable
(`chmod 700 ~/.skillcurrent/hook.sh`):

```sh
#!/bin/sh
# Brings installed skills back to what their channel serves.
# Output goes to a log file, never into an agent session.
. "$HOME/.skillcurrent/env"
{
  date -u +"%Y-%m-%dT%H:%M:%SZ"
  "$SKILLCURRENT_BIN" sync
} >> "$HOME/.skillcurrent/sync.log" 2>&1
exit 0
```

**Claude Code** runs it when a session starts. Add this to
`~/.claude/settings.json`, merging with any `hooks` you already have:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup",
        "hooks": [
          { "type": "command", "command": "$HOME/.skillcurrent/hook.sh" }
        ]
      }
    ]
  }
}
```

**Machines where Claude Code isn't opened every day** (teams on Codex,
Antigravity, Osaurus or Cursor): run the same script hourly from cron
(`crontab -e`). It syncs every tool's copies, whichever one triggers it.

```
17 * * * * $HOME/.skillcurrent/hook.sh
```

Check that it works: start a session (or run the script by hand), then look
at the end of the log:

```bash
tail -n 20 ~/.skillcurrent/sync.log
```

`sync` repairs outdated and missing copies where they were installed. It
leaves a hand-modified copy alone and records that it did; `sync --force`
overwrites it. Nothing here reads or changes a file SkillCurrent did not
install.

## The weekly report

Every Monday, one maintainer runs:

```bash
skillcurrent --json beta-report --days 7 > beta-report.json
```

Read it, then attach it to a reply on your email thread with us. Please
don't post it in a public issue. Without `--json` the same report prints as
plain text with the two answers at the top:

- **Did the team keep review switched on?** `yes`, `partly`, `unclear` or
  `no activity`, with the reasons. "Partly" means something routed around
  review: an unreviewed draft was installed, the server ran with
  `--no-auth`, or approvals were nearly always instant and nothing was ever
  rejected.
- **Did sync ever catch a bad copy?** `yes`, `no bad copy seen` or
  `never checked`. "Never checked" means no status or sync run was reported
  in the window, which usually means the hook is not running. Outdated
  copies after a new release are counted separately; they are expected.

## Upgrades

We'll tell you on your email thread when there's a fix worth taking, with
what changed. Back up first, then reinstall:

```bash
skillcurrent backup ~/skillcurrent-before-upgrade.sqlite     # on the server
pipx install --force "git+https://github.com/OpsToInnovator/agency-agents@main#subdirectory=examples/skillcurrent"
```

Restart `serve` afterwards. Database upgrades only add columns and tables,
so an upgrade never deletes what your team has recorded.

## Known limits

- Tokens are the whole authentication story: no SSO and no expiry. Rotate
  one with `skillcurrent members rotate-token <handle>`.
- One `serve` process per database file.
- `sync` only manages copies that SkillCurrent installed. A skill copied
  into a tool's folder by hand is invisible to it.
- A passing check is evidence about specific bytes. It is not a guarantee of
  how an agent will behave.
- Windows is untested.

## Leaving the beta

Tell us on your email thread, then:

```bash
crontab -e                          # remove the hook line, if you added one
# remove the SessionStart entry from ~/.claude/settings.json
pipx uninstall skillcurrent
rm -rf ~/.skillcurrent
```

Installed skills stay where they are as plain `SKILL.md` files. Your
database is yours to keep or delete. Ask, and we'll delete your sign-up and
the reports you sent us.

## Help

Reply on your email thread with us. For a security problem, follow
[SECURITY.md](SECURITY.md) instead of writing in the open.
