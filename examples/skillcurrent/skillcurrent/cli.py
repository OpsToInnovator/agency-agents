"""Command-line interface. ``python -m skillcurrent --help``."""

import argparse
import csv
import json
import logging
import os
import shutil
import sys
from pathlib import Path

from . import __version__, checks, importer, skillfile
from .errors import Invalid, SkillCurrentError
from .installer import DEFAULT_TARGET, TARGETS, Installer
from .semver import BUMPS
from .service import CHANNELS, DEFAULT_CHANNEL, RECEIPT_EVENTS, ROLES, STATUSES, Service
from .session import LocalSession, RemoteSession, require_identity
from .store import Store

DEFAULT_DB = "~/.skillcurrent/skillcurrent.db"


# ----------------------------------------------------------------------- output
class Out:
    def __init__(self, as_json: bool):
        self.as_json = as_json

    def emit(self, data, human=None):
        if self.as_json:
            print(json.dumps(data, indent=2, ensure_ascii=False))
        elif human is not None:
            print(human() if callable(human) else human)
        elif isinstance(data, str):
            print(data)
        else:
            print(json.dumps(data, indent=2, ensure_ascii=False))


def table(rows: list[dict], columns: list[tuple[str, str]]) -> str:
    if not rows:
        return "(none)"
    widths = {key: len(label) for key, label in columns}
    cells = []
    for row in rows:
        cell = {}
        for key, _ in columns:
            value = row.get(key)
            if isinstance(value, list):
                value = ", ".join(str(v) for v in value)
            text = "" if value is None else str(value)
            cell[key] = text
            widths[key] = max(widths[key], len(text))
        cells.append(cell)
    lines = ["  ".join(label.ljust(widths[key]) for key, label in columns)]
    lines.append("  ".join("-" * widths[key] for key, _ in columns))
    for cell in cells:
        lines.append("  ".join(cell[key].ljust(widths[key]) for key, _ in columns).rstrip())
    return "\n".join(lines)


def read_content(path: str) -> str:
    p = Path(path)
    if p.is_dir():
        p = p / "SKILL.md"
    if str(path) == "-":
        return sys.stdin.read()
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------- session
def open_store(args) -> Store:
    return Store(Path(args.db).expanduser())


def make_session(args):
    if args.server:
        session = RemoteSession(args.server, token=args.token, team=args.team, actor=args.user)
        require_identity(session)
        return session
    if not args.team:
        raise Invalid("a team is required: pass --team or set SKILLCURRENT_TEAM")
    if not args.user:
        raise Invalid("an actor is required: pass --as or set SKILLCURRENT_USER")
    return LocalSession(Service(open_store(args), waitlist_team=os.environ.get("SKILLCURRENT_WAITLIST_TEAM")), args.team, args.user)


# --------------------------------------------------------------------- commands
def cmd_init(args, out):
    slug = args.init_team or args.team
    if not slug:
        raise Invalid("a team slug is required: skillcurrent init --team <slug> --owner <handle>")
    service = Service(open_store(args))
    result = service.create_team(slug, args.name or slug, args.owner, args.display)
    out.emit(
        result,
        lambda: (
            f"Created team {result['team']['slug']!r} with owner {result['owner']!r}.\n"
            f"Owner token (shown once, needed for `--server` access):\n  {result['token']}\n\n"
            f"Next: export SKILLCURRENT_TEAM={result['team']['slug']} SKILLCURRENT_USER={result['owner']}"
        ),
    )


def cmd_teams(args, out):
    teams = Service(open_store(args)).list_teams()
    out.emit(teams, lambda: table(teams, [("slug", "SLUG"), ("name", "NAME"), ("created_at", "CREATED")]))


def cmd_whoami(args, out):
    info = make_session(args).call("whoami")
    out.emit(info, lambda: f"{info['member']['handle']} ({info['member']['role']}) in team {info['team']['slug']} — {info['team']['name']}")


def cmd_members(args, out):
    session = make_session(args)
    sub = args.members_cmd
    if sub == "list" or sub is None:
        rows = session.call("list_members")
        out.emit(rows, lambda: table(rows, [("handle", "HANDLE"), ("display_name", "NAME"), ("role", "ROLE"), ("created_at", "SINCE")]))
    elif sub == "add":
        m = session.call("add_member", handle=args.handle, role=args.role, display_name=args.display)
        out.emit(m, lambda: f"Added {m['handle']} as {m['role']}.\nToken (shown once):\n  {m['token']}")
    elif sub == "role":
        m = session.call("set_role", handle=args.handle, role=args.role)
        out.emit(m, lambda: f"{m['handle']} is now a {m['role']}.")
    elif sub == "remove":
        r = session.call("remove_member", handle=args.handle)
        extra = f" Reassigned skills: {', '.join(r['reassigned_skills'])}" if r["reassigned_skills"] else ""
        out.emit(r, lambda: f"Removed {r['removed']}.{extra}")
    elif sub == "rotate-token":
        r = session.call("rotate_token", handle=args.handle)
        out.emit(r, lambda: f"New token for {r['handle']} (shown once):\n  {r['token']}")


def _skill_line(s: dict) -> dict:
    checks = s.get("checks")
    return {
        **s,
        "production": s["production_version"] or "-",
        "canary": s["channels"].get("canary", {}).get("version", "") if s.get("channels") else "",
        "approved": s["latest_version"] or "-",
        "draft": "yes" if s["has_draft"] else "",
        "checks": ("pass" if checks["passed"] else f"fail {checks['failed']}") + ("" if checks["current"] else " (stale)") if checks and s["has_draft"] else "",
    }


def cmd_skills(args, out):
    session = make_session(args)
    sub = args.skills_cmd
    if sub == "list" or sub is None:
        rows = [_skill_line(s) for s in session.call("list_skills", status=args.status, tag=args.tag, query=args.query)]
        out.emit(
            rows,
            lambda: table(rows, [("slug", "SKILL"), ("status", "STATUS"), ("production", "PRODUCTION"), ("canary", "CANARY"), ("approved", "APPROVED"), ("draft", "DRAFT"), ("checks", "CHECKS"), ("owner", "OWNER"), ("installs", "INSTALLS"), ("tags", "TAGS")]),
        )
    elif sub == "show":
        s = session.call("get_skill", slug=args.slug)

        def human():
            lines = [
                f"{s['slug']}  [{s['status']}]  {s['title']}",
                f"  {s['description']}",
                f"  owner: {s['owner']}   approved: {s['latest_version'] or '-'}   installs: {s.get('installs', 0)}   tags: {', '.join(s['tags']) or '-'}",
            ]
            for name, c in s["channels"].items():
                lines.append(f"  {name}: {c['version']}  sha256 {c['content_hash'][:16]}…  released by {c['set_by']} at {c['set_at']}")
            if s["has_draft"]:
                lines.append(f"  draft by {s['draft_editor']} at {s['draft_updated_at']}")
                c = s["checks"]
                if c and c["current"]:
                    lines.append(f"  checks: {'passed' if c['passed'] else str(c['failed']) + ' failing'} ({c['total']} run by {c['run_by']} at {c['run_at']})")
                else:
                    lines.append("  checks: not run on this draft (skills checks " + s["slug"] + ")")
            if s["pending_review"]:
                r = s["pending_review"]
                lines.append(f"  in review: submitted by {r['submitted_by']} ({r['bump']} bump) — {r['note'] or 'no note'}")
            if s["lifecycle"] == "deprecated":
                lines.append(f"  DEPRECATED: {s['deprecation_reason'] or 'no reason given'}")
            if s["versions"]:
                lines.append("  versions:")
                for v in s["versions"]:
                    lines.append(f"    {v['version']:<10} {v['created_at']}  by {v['author']}, approved by {v['approved_by']}  {v['note']}")
            return "\n".join(lines)

        out.emit(s, human)
    elif sub == "cat":
        c = session.call("get_content", slug=args.slug, ref=args.ref)
        out.emit(c, lambda: c["content"].rstrip("\n"))
    elif sub == "new":
        if args.file:
            content = read_content(args.file)
        elif args.name:
            content = skillfile.template(args.name, args.description or skillfile.template.__defaults__[0])
        else:
            raise Invalid("pass a SKILL.md path or --name to start from the template")
        s = session.call("create_skill", content=content)
        out.emit(s, lambda: f"Created draft {s['slug']} (owner {s['owner']}). Submit it with: skills submit {s['slug']}")
    elif sub == "edit":
        s = session.call("update_draft", slug=args.slug, content=read_content(args.file))
        out.emit(s, lambda: f"Draft of {s['slug']} updated.")
    elif sub == "discard":
        s = session.call("discard_draft", slug=args.slug)
        out.emit(s, lambda: f"Draft of {s['slug']} discarded; approved version {s['latest_version']} stands.")
    elif sub == "delete":
        r = session.call("delete_skill", slug=args.slug)
        out.emit(r, lambda: f"Deleted {r['deleted']}.")
    elif sub == "validate":
        content = read_content(args.file)
        problems, warnings, doc = skillfile.check(content)
        report = checks.run(content)
        result = {"ok": not problems and report["passed"], "problems": problems, "warnings": warnings, "name": doc.name if doc else None, "checks": report}

        def human():
            lines = [f"{'OK' if result['ok'] else 'NEEDS ATTENTION'}: {args.file}"]
            lines += [f"  problem: {p}" for p in problems]
            lines += [f"  warning: {w}" for w in warnings]
            lines.append(checks_table(report))
            return "\n".join(lines)

        out.emit(result, human)
        return 0 if result["ok"] else 1
    elif sub == "checks":
        report = session.call("run_checks", slug=args.slug)
        out.emit(report, lambda: f"{args.slug}: {'all ' + str(report['total']) + ' checks passed' if report['passed'] else str(report['failed']) + ' of ' + str(report['total']) + ' checks failed'}\n" + checks_table(report))
        return 0 if report["passed"] else 1
    elif sub == "submit":
        r = session.call("submit_review", slug=args.slug, bump=args.bump, note=args.note or "")
        out.emit(r, lambda: f"Submitted {r['skill']} for review as version {r['proposed_version']} ({r['bump']} bump).")
    elif sub == "withdraw":
        s = session.call("withdraw_review", slug=args.slug)
        out.emit(s, lambda: f"Review of {s['slug']} withdrawn; it is back to {s['status']}.")
    elif sub == "approve":
        s = session.call("approve", slug=args.slug, comment=args.comment or "", release=args.release)
        v = s["approved"]

        def human():
            line = f"Approved {s['slug']} {v['version']} (authored by {v['author']}); sha256 {v['content_hash']}"
            if args.release:
                return line + f"\nReleased to {args.release}."
            return line + f"\nNothing is installed yet. Release it with: skills release {s['slug']} --channel canary|production"

        out.emit(s, human)
    elif sub == "release":
        r = session.call("release", slug=args.slug, version=args.version, channel=args.channel, note=args.note or "")
        out.emit(r, lambda: f"{r['slug']} {r['version']} is now the {r['channel']} release" + (f" (was {r['previous']})." if r["previous"] else "."))
    elif sub == "rollback":
        r = session.call("rollback", slug=args.slug, channel=args.channel, reason=args.reason or "")
        out.emit(r, lambda: f"Rolled {r['channel']} back to {r['slug']} {r['version']} (was {r['previous']}). Installed copies are unchanged until members run sync.")
    elif sub == "history":
        hist = session.call("release_history", slug=args.slug)

        def human():
            lines = [f"{args.slug}: " + (", ".join(f"{k} = {v['version']}" for k, v in hist["channels"].items()) or "no releases yet")]
            lines.append(table(hist["versions"], [("version", "VERSION"), ("created_at", "APPROVED"), ("author", "AUTHOR"), ("approved_by", "APPROVED BY"), ("content_hash", "SHA256"), ("note", "NOTE")]))
            if hist["events"]:
                lines.append("")
                lines.append(table(hist["events"], [("at", "AT"), ("kind", "EVENT"), ("channel", "CHANNEL"), ("version", "VERSION"), ("previous_version", "WAS"), ("by", "BY"), ("reason", "REASON")]))
            return "\n".join(lines)

        out.emit(hist, human)
    elif sub == "passport":
        p = session.call("passport", slug=args.slug)

        def human():
            lines = [f"{p['canonical_id']}  [{p['status']}]  {p['title']}", f"  {p['description']}", f"  owner {p['owner']}   tags {', '.join(p['tags']) or '-'}   approved {p['latest_version'] or '-'}"]
            for name, c in p["channels"].items():
                lines.append(f"  {name}: {c['version']}  sha256 {c['content_hash']}")
            e = p["environments"]
            lines.append(f"  environments {e['environments']}   current {e['current']}   verified {e['verified']}   loaded {e['loaded']}   task-tested {e['task_tested']}   needs attention {e['needs_attention']}")
            lines.append(f"  team rules applied: {', '.join(p['rules']) or 'none (built-in checks only)'}")
            return "\n".join(lines)

        out.emit(p, human)
    elif sub == "reject":
        s = session.call("reject", slug=args.slug, reason=args.reason)
        out.emit(s, lambda: f"Rejected {s['slug']}; the draft stays open for changes.")
    elif sub == "deprecate":
        s = session.call("deprecate", slug=args.slug, reason=args.reason or "")
        out.emit(s, lambda: f"Deprecated {s['slug']}.")
    elif sub == "restore":
        s = session.call("restore", slug=args.slug)
        out.emit(s, lambda: f"Restored {s['slug']} ({s['status']}).")
    elif sub == "owner":
        s = session.call("set_owner", slug=args.slug, handle=args.handle)
        out.emit(s, lambda: f"{s['slug']} is now owned by {s['owner']}.")
    elif sub == "versions":
        rows = session.call("list_versions", slug=args.slug)
        out.emit(rows, lambda: table(rows, [("version", "VERSION"), ("created_at", "PUBLISHED"), ("author", "AUTHOR"), ("approved_by", "APPROVED BY"), ("note", "NOTE")]))
    elif sub == "diff":
        d = session.call("diff", slug=args.slug, from_ref=args.from_ref, to_ref=args.to_ref)
        out.emit(d, lambda: d["diff"].rstrip("\n") or f"(no differences between {d['from']} and {d['to']})")
    return 0


def cmd_reviews(args, out):
    rows = make_session(args).call("pending_reviews")
    out.emit(rows, lambda: table(rows, [("skill", "SKILL"), ("proposed_version", "PROPOSED"), ("bump", "BUMP"), ("submitted_by", "BY"), ("created_at", "SUBMITTED"), ("note", "NOTE")]))


def checks_table(report: dict) -> str:
    rows = [{**r, "result": "PASS" if r["passed"] else "FAIL"} for r in report["results"]]
    return table(rows, [("result", "RESULT"), ("name", "CHECK"), ("category", "CATEGORY"), ("detail", "DETAIL")])


def _installer(args) -> Installer:
    return Installer(make_session(args), project=Path(args.project) if args.project else None, custom_dir=args.dir, host=args.host)


def cmd_install(args, out):
    inst = _installer(args)
    results = [inst.install(slug, args.target, ref=args.ref, channel=args.channel) for slug in args.slugs]

    def human():
        lines = []
        for r in results:
            flag = "  (DEPRECATED — consider removing it)" if r["deprecated"] else ""
            lines.append(f"Installed {r['slug']} {r['version']} → {r['path']}{flag}")
        return "\n".join(lines)

    out.emit(results, human)


def cmd_uninstall(args, out):
    inst = _installer(args)
    results = [inst.uninstall(slug, args.target) for slug in args.slugs]
    out.emit(results, lambda: "\n".join(f"Removed {r['slug']} from {r['target']}" for r in results))


def cmd_status(args, out):
    rows = _installer(args).status(args.target, report=not args.no_report)
    out.emit(rows, lambda: table(rows, [("slug", "SKILL"), ("target", "TARGET"), ("channel", "CHANNEL"), ("installed", "INSTALLED"), ("target_version", "CHANNEL HAS"), ("state", "STATE"), ("path", "PATH")]))
    return 0 if all(r["state"] in ("current", "deprecated", "unreleased") for r in rows) else 2


def cmd_sync(args, out):
    rows = _installer(args).sync(args.target, force=args.force)
    out.emit(rows, lambda: table(rows, [("slug", "SKILL"), ("target", "TARGET"), ("channel", "CHANNEL"), ("action", "ACTION"), ("installed", "INSTALLED"), ("target_version", "CHANNEL HAS"), ("reason", "NOTE")]))


def cmd_report(args, out):
    r = _installer(args).report(args.slug, args.event, args.target, detail=args.detail or "")
    out.emit(r, lambda: f"Recorded {r['event']} receipt for {r['slug']} {r['version']} on {r['target']} ({r['host'] or 'default host'}).")


def cmd_adoption(args, out):
    a = make_session(args).call("adoption", slug=args.slug)

    def human():
        s = a["summary"]
        head = f"{a['slug'] or 'all skills'}: {s['environments']} environments — installed {s['installed']}, verified {s['verified']}, loaded {s['loaded']}, task-tested {s['task_tested']}; current {s['current']}, needs attention {s['needs_attention']}"
        rows = a["environments"]
        if args.attention:
            rows = [e for e in rows if e["needs_attention"]]
        return head + "\n" + table(rows, [("environment", "ENVIRONMENT"), ("runtime", "RUNTIME"), ("slug", "SKILL"), ("version", "VERSION"), ("target_version", "CHANNEL HAS"), ("evidence", "EVIDENCE"), ("state", "STATE"), ("last_seen", "LAST SEEN")])

    out.emit(a, human)


def cmd_rules(args, out):
    session = make_session(args)
    sub = args.rules_cmd
    if sub == "list" or sub is None:
        rows = session.call("list_rules")
        out.emit(rows, lambda: table(rows, [("name", "RULE"), ("kind", "KIND"), ("pattern", "PATTERN"), ("category", "CATEGORY"), ("rationale", "RATIONALE"), ("created_by", "BY")]) if rows else "(no team rules; the built-in checks still apply)")
    elif sub == "add":
        r = session.call("add_rule", name=args.name, kind=args.kind, pattern=args.pattern, category=args.category, rationale=args.rationale or "")
        out.emit(r, lambda: f"Added rule {r['name']} ({r['kind']} /{r['pattern']}/). It applies to every future check run.")
    elif sub == "remove":
        r = session.call("remove_rule", name=args.name)
        out.emit(r, lambda: f"Removed rule {r['removed']}.")


def cmd_import(args, out):
    rows = importer.import_path(make_session(args), args.path, update=args.update)
    out.emit(rows, lambda: table(rows, [("action", "ACTION"), ("slug", "SKILL"), ("source", "SOURCE"), ("reason", "NOTE")]))


def cmd_activity(args, out):
    rows = make_session(args).call("activity", limit=args.limit, slug=args.slug, action=args.action)

    def human():
        lines = []
        for a in rows:
            details = ", ".join(f"{k}={v}" for k, v in a["details"].items() if v not in ("", None, []))
            lines.append(f"{a['at']}  {a['actor']:<14} {a['action']:<22} {a['skill'] or '':<24} {details}".rstrip())
        return "\n".join(lines) or "(no activity yet)"

    out.emit(rows, human)


def cmd_dashboard(args, out):
    d = make_session(args).call("dashboard")

    def human():
        s = d["by_status"]
        lines = [
            f"{d['team']['name']} ({d['team']['slug']}) — {d['members']} members, {d['skills_total']} skills",
            f"  published {s['published']}   approved (unreleased) {s['approved']}   in review {s['in_review']}   drafts {s['draft']}   deprecated {s['deprecated']}",
        ]
        if d["pending_reviews"]:
            lines.append("  waiting for review:")
            lines += [f"    {r['skill']} → {r['proposed_version']} (by {r['submitted_by']})" for r in d["pending_reviews"]]
        if d["most_installed"]:
            lines.append("  most installed:")
            lines += [f"    {m['slug']}: {m['installs']}" for m in d["most_installed"]]
        return "\n".join(lines)

    out.emit(d, human)


def cmd_index(args, out):
    idx = make_session(args).call("catalog_index")

    def human():
        lines = [f"generation {idx['generation']}   index sha256 {idx['index_hash']}   generated {idx['generated_at']}"]
        lines.append(table(idx["skills"], [("slug", "SKILL"), ("channel", "CHANNEL"), ("version", "VERSION"), ("lifecycle", "LIFECYCLE"), ("released_at", "RELEASED"), ("content_hash", "SHA256")]))
        return "\n".join(lines)

    out.emit(idx, human)


def _read_signup_rows(path: str) -> list[dict]:
    """Rows from a CSV export (any column order; header names matched loosely) or a JSON list."""
    text = Path(path).expanduser().read_text(encoding="utf-8-sig")
    if path.lower().endswith(".json"):
        data = json.loads(text)
        return data if isinstance(data, list) else []
    aliases = {
        "email": ("email", "work email", "work_email", "e-mail"),
        "team_size": ("team_size", "team size", "teamsize"),
        "tools": ("tools", "tools in use", "tools_in_use"),
        "note": ("note", "notes", "anything we should know?", "message"),
        "source": ("source", "ref"),
        "created_at": ("created_at", "created at", "date", "submitted_at", "timestamp"),
    }
    rows = []
    for raw in csv.DictReader(text.splitlines()):
        lowered = {str(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        rows.append({field: next((lowered[n] for n in names if lowered.get(n)), "") for field, names in aliases.items()})
    return rows


def cmd_beta(args, out):
    session = make_session(args)
    sub = getattr(args, "beta_cmd", None)
    if sub == "remove":
        r = session.call("remove_beta_signup", email=args.email)
        out.emit(r, lambda: f"Removed {r['email']}." if r["removed"] else f"No sign-up for {r['email']}.")
        return 0 if r["removed"] else 1
    if sub == "import":
        r = session.call("import_beta_signups", rows=_read_signup_rows(args.file))
        out.emit(r, lambda: f"Imported: {r['added']} new, {r['merged']} merged into existing sign-ups, {r['skipped']} skipped (invalid email).")
        return 0
    rows = session.call("list_beta_signups")
    if getattr(args, "sources", False):
        counts: dict[str, int] = {}
        for r in rows:
            counts[r["source"] or "(none)"] = counts.get(r["source"] or "(none)", 0) + 1
        summary = [{"source": k, "signups": v} for k, v in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
        out.emit(summary, lambda: table(summary, [("signups", "SIGN-UPS"), ("source", "SOURCE")]) if summary else "(no beta sign-ups yet)")
        return 0
    out.emit(rows, lambda: table(rows, [("email", "EMAIL"), ("team_size", "TEAM"), ("tools", "TOOLS"), ("source", "SOURCE"), ("submissions", "POSTS"), ("created_at", "SIGNED UP"), ("note", "NOTE")]) if rows else "(no beta sign-ups yet)")


def cmd_beta_report(args, out):
    r = make_session(args).call("beta_report", days=args.days)

    def human():
        rv, sy, ad, ans = r["review"], r["sync"], r["adoption"], r["answers"]
        ck = sy["checks"]
        window = f"last {r['window_days']} days" if r["window_days"] else "all time"
        lines = [
            f"SkillCurrent beta report for team {r['team']} ({window}), generated {r['generated_at']}",
            f"auth mode: {r['auth_mode']}   members: {r['members']}   skills: {r['skills']}   schema: v{r['schema_version']}",
            "",
            f"1. Did the team keep review switched on?  {ans['kept_review_on']['verdict'].upper()}",
            *[f"   - {b}" for b in ans["kept_review_on"]["because"]],
            f"   submitted {rv['submitted']}, approved {rv['approved']}, rejected {rv['rejected']}, withdrawn {rv['withdrawn']}, pending {rv['pending']}",
            f"   approvers {rv['distinct_approvers']}, submitters {rv['distinct_submitters']}, median submit-to-approve "
            + (f"{rv['median_seconds_submit_to_approve']}s" if rv["median_seconds_submit_to_approve"] is not None else "n/a")
            + f", approvals under 60s {rv['approvals_under_60_seconds']}",
            f"   releases {rv['releases']}, rollbacks {rv['rollbacks']}, draft installs {rv['draft_installs']}",
            "",
            f"2. Did sync ever catch a bad copy?  {ans['sync_caught_bad_copy']['verdict'].upper()}",
            *[f"   - {b}" for b in ans["sync_caught_bad_copy"]["because"]],
            f"   checks: {ck['environments_checked']} environment(s), {ck['days_with_checks']} day(s), {ck['verified_receipts']} verified receipt(s), last "
            + (ck["last_check_at"] or "never"),
            f"   bad copies {sy['bad_copies']} (modified {sy['by_state']['modified']}, missing {sy['by_state']['missing']}); "
            f"outdated {sy['by_state']['outdated']}; kept local {sy['by_response']['kept_local']}, repaired {sy['by_response']['repaired']}, "
            f"forced {sy['by_response']['forced']}",
            "",
            f"Adoption now, by the versions on record: {ad['environments']} environments; installed {ad['installed']}, verified {ad['verified']}, "
            f"loaded {ad['loaded']}, task-tested {ad['task_tested']}; on the channel's version {ad['current']}, behind or deprecated {ad['needs_attention']}",
            "(A hand-edited copy still has its recorded version; question 2 is where edits and deletions show.)",
            "",
            "This report holds counts only: no skill content, member names or emails. Send it with --json to your beta contact.",
        ]
        return "\n".join(lines)

    out.emit(r, human)


def cmd_backup(args, out):
    dest = open_store(args).backup(args.dest)
    out.emit({"backup": str(dest)}, lambda: f"Wrote a consistent copy of {Path(args.db).expanduser()} to {dest}")


def cmd_build_landing(args, out):
    """Render the beta page for a static host (Netlify, GitHub Pages, Cloudflare Pages, any file server)."""
    from .server import ASSETS_DIR, LANDING, render_landing, valid_endpoint

    if not args.site_url.startswith(("https://", "http://")):
        raise Invalid("--site-url must be the page's public address, e.g. https://beta.example.com")
    endpoint = args.endpoint
    if not valid_endpoint(endpoint):
        raise Invalid("--endpoint must be '/api/beta', an https URL of a skillcurrent server, 'netlify' or 'mailto'")
    if endpoint == "mailto" and not args.contact:
        raise Invalid("--endpoint mailto needs --contact: sign-ups are sent by email to that address")
    dest = Path(args.out).expanduser()
    dest.mkdir(parents=True, exist_ok=True)
    page = render_landing(LANDING.read_text(encoding="utf-8"), endpoint=endpoint, site_url=args.site_url, contact=args.contact or "")
    (dest / "index.html").write_text(page, encoding="utf-8")
    shutil.copyfile(ASSETS_DIR / "og.png", dest / "og.png")
    (dest / "fonts").mkdir(exist_ok=True)
    for font in sorted((ASSETS_DIR / "fonts").glob("*.woff2")):
        shutil.copyfile(font, dest / "fonts" / font.name)
    shutil.copyfile(ASSETS_DIR / "fonts" / "OFL.txt", dest / "fonts" / "OFL.txt")  # the font licence travels with the fonts
    # Security headers for hosts that read a _headers file (Netlify, Cloudflare Pages).
    connect = "'self'"
    if endpoint.startswith(("https://", "http://")):
        from urllib.parse import urlsplit

        parts = urlsplit(endpoint)
        connect += f" {parts.scheme}://{parts.netloc}"
    csp = (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        f"font-src 'self'; connect-src {connect}; form-action 'self' mailto:; frame-ancestors 'none'; base-uri 'none'; object-src 'none'"
    )
    (dest / "_headers").write_text(
        "/*\n"
        f"  Content-Security-Policy: {csp}\n"
        "  X-Content-Type-Options: nosniff\n"
        "  Referrer-Policy: strict-origin-when-cross-origin\n"
        "  X-Frame-Options: DENY\n",
        encoding="utf-8",
    )
    files = sorted(str(p.relative_to(dest)) for p in dest.rglob("*") if p.is_file())
    out.emit({"out": str(dest), "endpoint": endpoint, "files": files}, lambda: f"Wrote the beta page for {args.site_url} (form endpoint: {endpoint}) to {dest}:\n  " + "\n  ".join(files))


def cmd_targets(args, out):
    rows = [{"id": t.id, "label": t.label, "scope": t.scope, "base": t.base or "(--dir)"} for t in TARGETS.values()]
    out.emit(rows, lambda: table(rows, [("id", "TARGET"), ("label", "LABEL"), ("scope", "SCOPE"), ("base", "DIRECTORY")]))


def cmd_serve(args, out):
    from .server import LOOPBACK, make_server

    if args.no_auth and args.host not in LOOPBACK:
        raise Invalid(
            f"--no-auth trusts two request headers as identity, so anyone who can reach {args.host} could act as any member. "
            "Use it only with --host 127.0.0.1."
        )
    if args.no_auth and args.public:
        raise Invalid("--public and --no-auth cannot be combined")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    service = Service(open_store(args), waitlist_team=args.waitlist_team)
    server = make_server(
        service, args.host, args.port, no_auth=args.no_auth, verbose=args.verbose,
        public=args.public, allow_origins=args.allow_origin or (), trust_proxy=args.trust_proxy,
        contact=args.contact or "", site_url=args.site_url or "",
        # A request log records client addresses. The public waitlist host keeps none unless asked to.
        access_log=not args.quiet and (args.access_log or not args.public),
    )
    mode = "no auth (identify with X-SkillCurrent-Team / X-SkillCurrent-User)" if args.no_auth else "member tokens"
    if args.public:
        mode += "; public mode: beta page and waitlist only"
    mode += "; request log " + ("on (records client addresses)" if server.RequestHandlerClass.access_log else "off")
    print(f"SkillCurrent {__version__} serving {Path(args.db).expanduser()} at http://{args.host}:{server.server_address[1]}/  [{mode}]", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


# ----------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="skillcurrent", description="Shared skill management for teams.")
    p.add_argument("--version", action="version", version=f"skillcurrent {__version__}")
    p.add_argument("--db", default=os.environ.get("SKILLCURRENT_DB", DEFAULT_DB), help="SQLite file (local mode) [SKILLCURRENT_DB]")
    p.add_argument("--team", default=os.environ.get("SKILLCURRENT_TEAM"), help="team slug [SKILLCURRENT_TEAM]")
    p.add_argument("--as", dest="user", default=os.environ.get("SKILLCURRENT_USER"), help="act as this member handle [SKILLCURRENT_USER]")
    p.add_argument("--server", default=os.environ.get("SKILLCURRENT_SERVER"), help="use a running `skillcurrent serve` instead of a local DB [SKILLCURRENT_SERVER]")
    p.add_argument("--token", default=os.environ.get("SKILLCURRENT_TOKEN"), help="member token for --server [SKILLCURRENT_TOKEN]")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="create a team and its first owner in the local database")
    s.add_argument("--team", dest="init_team", help="team slug (or use the global --team)")
    s.add_argument("--name")
    s.add_argument("--owner", required=True, help="handle of the first owner")
    s.add_argument("--display")
    s.set_defaults(fn=cmd_init)

    sub.add_parser("teams", help="list teams in the local database").set_defaults(fn=cmd_teams)
    sub.add_parser("whoami", help="show the current member and role").set_defaults(fn=cmd_whoami)

    m = sub.add_parser("members", help="list and manage members")
    ms = m.add_subparsers(dest="members_cmd")
    ms.add_parser("list")
    a = ms.add_parser("add")
    a.add_argument("handle")
    a.add_argument("--role", choices=ROLES, default="contributor")
    a.add_argument("--display")
    r = ms.add_parser("role")
    r.add_argument("handle")
    r.add_argument("role", choices=ROLES)
    ms.add_parser("remove").add_argument("handle")
    ms.add_parser("rotate-token").add_argument("handle", nargs="?")
    m.set_defaults(fn=cmd_members)

    k = sub.add_parser("skills", help="browse and manage the catalog")
    ks = k.add_subparsers(dest="skills_cmd")
    lst = ks.add_parser("list")
    lst.add_argument("--status", choices=STATUSES)
    lst.add_argument("--tag")
    lst.add_argument("-q", "--query")
    ks.add_parser("show").add_argument("slug")
    cat = ks.add_parser("cat", help="print a skill's content")
    cat.add_argument("slug")
    cat.add_argument("--ref", help="'latest' (default), 'draft' or a version")
    new = ks.add_parser("new", help="create a draft from a SKILL.md file or the template")
    new.add_argument("file", nargs="?", help="SKILL.md path, a skill directory, or '-' for stdin")
    new.add_argument("--name", help="start from the template with this slug")
    new.add_argument("--description")
    ed = ks.add_parser("edit", help="replace the draft with a file's content")
    ed.add_argument("slug")
    ed.add_argument("file")
    ks.add_parser("discard").add_argument("slug")
    ks.add_parser("delete").add_argument("slug")
    ks.add_parser("validate", help="parse a SKILL.md and run the built-in checks offline").add_argument("file")
    ks.add_parser("checks", help="run the checks on the current draft and record the result (required before submit)").add_argument("slug")
    sb = ks.add_parser("submit", help="submit the draft for review")
    sb.add_argument("slug")
    sb.add_argument("--bump", choices=BUMPS, default="minor")
    sb.add_argument("--note")
    ks.add_parser("withdraw").add_argument("slug")
    ap = ks.add_parser("approve", help="approve the pending review: freeze an immutable version (release separately, or with --release)")
    ap.add_argument("slug")
    ap.add_argument("--comment")
    ap.add_argument("--release", choices=CHANNELS, help="also release the new version to this channel")
    rl = ks.add_parser("release", help="point a channel at an approved version")
    rl.add_argument("slug")
    rl.add_argument("version", nargs="?", help="defaults to the newest approved version")
    rl.add_argument("--channel", choices=CHANNELS, default=DEFAULT_CHANNEL)
    rl.add_argument("--note")
    rb = ks.add_parser("rollback", help="point a channel back at its previous version")
    rb.add_argument("slug")
    rb.add_argument("--channel", choices=CHANNELS, default=DEFAULT_CHANNEL)
    rb.add_argument("--reason")
    ks.add_parser("history", help="approved versions, digests and channel moves").add_argument("slug")
    ks.add_parser("passport", help="identity, ownership, releases and adoption in one card").add_argument("slug")
    rj = ks.add_parser("reject")
    rj.add_argument("slug")
    rj.add_argument("--reason", required=True)
    dp = ks.add_parser("deprecate")
    dp.add_argument("slug")
    dp.add_argument("--reason")
    ks.add_parser("restore").add_argument("slug")
    ow = ks.add_parser("owner", help="transfer a skill to another member")
    ow.add_argument("slug")
    ow.add_argument("handle")
    ks.add_parser("versions").add_argument("slug")
    df = ks.add_parser("diff")
    df.add_argument("slug")
    df.add_argument("--from", dest="from_ref", default="latest")
    df.add_argument("--to", dest="to_ref", default="draft")
    k.set_defaults(fn=cmd_skills)

    sub.add_parser("reviews", help="list submissions waiting for review").set_defaults(fn=cmd_reviews)
    sub.add_parser("index", help="published skills with versions and content digests (generation + index hash)").set_defaults(fn=cmd_index)

    def add_target_args(parser, with_target=True):
        if with_target:
            parser.add_argument("--target", choices=list(TARGETS), default=DEFAULT_TARGET)
        else:
            parser.add_argument("--target", choices=list(TARGETS))
        parser.add_argument("--dir", help="directory for --target custom")
        parser.add_argument("--project", help="project root for *-project targets (default: cwd)")
        parser.add_argument("--host", default=os.environ.get("SKILLCURRENT_HOST"), help="name of this machine (default: hostname) [SKILLCURRENT_HOST]")

    i = sub.add_parser("install", help="install a skill's release into a tool's skills directory")
    i.add_argument("slugs", nargs="+")
    i.add_argument("--channel", choices=CHANNELS, default=DEFAULT_CHANNEL, help="which release channel this environment follows")
    i.add_argument("--ref", help="override: 'latest' (newest approved), 'draft' or a version")
    add_target_args(i)
    i.set_defaults(fn=cmd_install)
    u = sub.add_parser("uninstall")
    u.add_argument("slugs", nargs="+")
    add_target_args(u)
    u.set_defaults(fn=cmd_uninstall)
    st = sub.add_parser("status", help="compare installed copies with disk and their channel (exit 2 if anything needs attention)")
    st.add_argument("--no-report", action="store_true", help="do not send 'verified' receipts for intact copies")
    add_target_args(st, with_target=False)
    st.set_defaults(fn=cmd_status)
    sy = sub.add_parser("sync", help="bring installs back to what their channel serves")
    sy.add_argument("--force", action="store_true", help="overwrite locally modified copies")
    add_target_args(sy, with_target=False)
    sy.set_defaults(fn=cmd_sync)
    rp = sub.add_parser("report", help="send a receipt for an installed skill (for tool hooks and test runners)")
    rp.add_argument("slug")
    rp.add_argument("event", choices=RECEIPT_EVENTS[1:])
    rp.add_argument("--detail")
    add_target_args(rp)
    rp.set_defaults(fn=cmd_report)
    ad = sub.add_parser("adoption", help="where each release landed, per environment, with its strongest receipt")
    ad.add_argument("--slug")
    ad.add_argument("--attention", action="store_true", help="only environments that need attention")
    ad.set_defaults(fn=cmd_adoption)
    ru = sub.add_parser("rules", help="team check rules applied on top of the built-in checks")
    rus = ru.add_subparsers(dest="rules_cmd")
    rus.add_parser("list")
    ra = rus.add_parser("add")
    ra.add_argument("name")
    ra.add_argument("kind", choices=checks.RULE_KINDS)
    ra.add_argument("pattern", help="case-insensitive regular expression")
    ra.add_argument("--category", choices=checks.CATEGORIES, default="team")
    ra.add_argument("--rationale")
    rus.add_parser("remove").add_argument("name")
    ru.set_defaults(fn=cmd_rules)
    sub.add_parser("targets", help="list install targets").set_defaults(fn=cmd_targets)
    bt = sub.add_parser("beta", help="the landing-page waitlist (owners of the waitlist team)")
    bts = bt.add_subparsers(dest="beta_cmd")
    bl = bts.add_parser("list", help="list sign-ups (the default)")
    bl.add_argument("--sources", action="store_true", help="count sign-ups by source tag (?c= and utm_ parameters)")
    bt.add_argument("--sources", action="store_true", help="count sign-ups by source tag (?c= and utm_ parameters)")
    bts.add_parser("remove", help="delete one sign-up (for deletion requests)").add_argument("email")
    bts.add_parser("import", help="merge sign-ups from a CSV or JSON export (e.g. a static host's form service)").add_argument("file")
    bt.set_defaults(fn=cmd_beta)
    br = sub.add_parser("beta-report", help="counts that answer the two beta questions; send this back to your beta contact")
    br.add_argument("--days", type=int, help="only the last N days (default: all time)")
    br.set_defaults(fn=cmd_beta_report)
    bk = sub.add_parser("backup", help="write a consistent copy of the database (safe while serving)")
    bk.add_argument("dest")
    bk.set_defaults(fn=cmd_backup)
    bl2 = sub.add_parser("build-landing", help="render the beta page and its assets for a static host")
    bl2.add_argument("out", help="output directory")
    bl2.add_argument("--site-url", required=True, help="the page's public address, used for link previews")
    bl2.add_argument("--endpoint", default="/api/beta", help="where the form posts: '/api/beta' (same host), an https URL of a skillcurrent server (needs --allow-origin there), 'netlify' (Netlify Forms), or 'mailto' (no server)")
    bl2.add_argument("--contact", help="the address sign-ups can write to; shown on the page")
    bl2.set_defaults(fn=cmd_build_landing)

    im = sub.add_parser("import", help="import SKILL.md folders or Agency agent files as drafts")
    im.add_argument("path")
    im.add_argument("--update", action="store_true", help="replace drafts of skills that already exist")
    im.set_defaults(fn=cmd_import)

    ac = sub.add_parser("activity", help="recent team activity")
    ac.add_argument("--limit", type=int, default=30)
    ac.add_argument("--slug")
    ac.add_argument("--action", help="only actions starting with this, e.g. drift. or review.")
    ac.set_defaults(fn=cmd_activity)
    sub.add_parser("dashboard", help="team overview").set_defaults(fn=cmd_dashboard)

    sv = sub.add_parser("serve", help="run the HTTP API and web UI")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8765)
    sv.add_argument("--no-auth", action="store_true", help="trust X-SkillCurrent-* headers instead of tokens (loopback demos only)")
    sv.add_argument("--public", action="store_true", help="internet-facing waitlist host: serve only the beta page, its assets and the sign-up endpoint")
    sv.add_argument("--allow-origin", action="append", metavar="ORIGIN", help="let a page on this origin post sign-ups cross-origin (repeatable), e.g. https://beta.example.com")
    sv.add_argument("--trust-proxy", action="store_true", help="read the client address from X-Forwarded-For (only behind your own proxy)")
    sv.add_argument("--contact", default=os.environ.get("SKILLCURRENT_CONTACT"), help="address shown on the beta page for replies and questions [SKILLCURRENT_CONTACT]")
    sv.add_argument("--site-url", default=os.environ.get("SKILLCURRENT_SITE_URL"), help="public https address of this server, for link previews [SKILLCURRENT_SITE_URL]")
    sv.add_argument("--waitlist-team", default=os.environ.get("SKILLCURRENT_WAITLIST_TEAM"), help="team whose owners may read the waitlist [SKILLCURRENT_WAITLIST_TEAM]")
    sv.add_argument("--access-log", action="store_true", help="log every request with the client's address (on by default, except with --public)")
    sv.add_argument("--quiet", action="store_true", help="no access log, whatever else is set")
    sv.add_argument("--verbose", action="store_true", help="(kept for compatibility; see --access-log and --quiet)")
    sv.set_defaults(fn=cmd_serve)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    out = Out(args.json)
    try:
        code = _run(args, out)
        sys.stdout.flush()  # surface a closed pipe here, not as a traceback at interpreter exit
        return code
    except BrokenPipeError:
        # The reader went away (`skillcurrent ... | head`), on success or while reporting an
        # error: stop quietly, the way Unix tools do.
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        return 141  # 128 + SIGPIPE


def _run(args, out) -> int:
    try:
        code = args.fn(args, out)
    except SkillCurrentError as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": {"code": exc.code, "message": exc.message}}))
        else:
            print(f"error ({exc.code}): {exc.message}", file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return int(code or 0)
