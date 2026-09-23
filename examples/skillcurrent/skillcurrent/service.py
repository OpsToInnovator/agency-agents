"""Business rules: roles, the draft → review → publish workflow, versions,
installs and the activity log.

Every operation that a team member can perform takes ``team`` (slug) and
``actor`` (member handle) as its first two arguments and is marked with
``@rpc`` so the HTTP server and the CLI can expose exactly the same surface.
Bootstrap operations (``create_team``, ``authenticate``) are not RPC-callable.
"""

import difflib
import hashlib
import json
import re
import secrets
import statistics
from datetime import datetime, timedelta, timezone

from . import checks, semver, skillfile
from .errors import Conflict, Forbidden, Invalid, NotFound
from .store import Store, now

ROLES = ("viewer", "contributor", "maintainer", "owner")
RANK = {role: i for i, role in enumerate(ROLES)}
STATUSES = ("draft", "in_review", "approved", "published", "deprecated")
CHANNELS = ("canary", "production")
DEFAULT_CHANNEL = "production"
RECEIPT_EVENTS = ("installed", "verified", "loaded", "task_tested")
EVIDENCE_RANK = {e: i for i, e in enumerate(RECEIPT_EVENTS)}
HANDLE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
SLUG_RE = skillfile.NAME_RE
TOKEN_PREFIX = "ts_"
# Values the beta form offers; anything else posted to the open endpoint is dropped.
BETA_TEAM_SIZES = ("1-4", "5-15", "16-50", "50+")
BETA_TOOLS = ("claude-code", "antigravity", "codex", "osaurus", "cursor", "other")
# What `status`/`sync` can find on disk, and what they did about it.
DRIFT_STATES = ("modified", "missing", "outdated")
BAD_COPY_STATES = ("modified", "missing")
DRIFT_RESPONSES = ("observed", "repaired", "forced", "kept_local")


def rpc(fn):
    fn.rpc = True
    return fn


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(24)


class Service:
    def __init__(self, store: Store, waitlist_team: str | None = None, auth_mode: str = "local"):
        self.store = store
        # The landing-page waitlist is instance-wide data, readable only by
        # owners of this one team (or of the only team, when there is one).
        self.waitlist_team = (waitlist_team or "").strip() or None
        # How callers are identified: "token" or "none" when served, "local" for the CLI on a DB file.
        self.auth_mode = auth_mode

    # ------------------------------------------------------------------ helpers
    def _team(self, slug: str) -> dict:
        team = self.store.team_by_slug(slug)
        if team is None:
            raise NotFound(f"team {slug!r} does not exist")
        return team

    def _member(self, team: dict, handle: str) -> dict:
        member = self.store.member(team["id"], handle)
        if member is None:
            raise Forbidden(f"{handle!r} is not a member of team {team['slug']!r}")
        return member

    def _ctx(self, team_slug: str, actor: str) -> tuple[dict, dict]:
        team = self._team(team_slug)
        return team, self._member(team, actor)

    @staticmethod
    def _require(member: dict, role: str, what: str) -> None:
        if RANK[member["role"]] < RANK[role]:
            raise Forbidden(f"{what} requires the {role} role; {member['handle']!r} is a {member['role']}")

    def _skill(self, team: dict, slug: str) -> dict:
        skill = self.store.skill(team["id"], slug)
        if skill is None:
            raise NotFound(f"skill {slug!r} does not exist in team {team['slug']!r}")
        return skill

    @staticmethod
    def _can_edit(member: dict, skill: dict) -> bool:
        if RANK[member["role"]] >= RANK["maintainer"]:
            return True
        return RANK[member["role"]] >= RANK["contributor"] and skill["owner_handle"] == member["handle"]

    def _require_edit(self, member: dict, skill: dict) -> None:
        if not self._can_edit(member, skill):
            raise Forbidden(
                f"editing {skill['slug']!r} requires being its owner ({skill['owner_handle']}) with the contributor role, or a maintainer"
            )

    def _log(self, team: dict, actor: str, action: str, slug: str | None = None, **details) -> None:
        self.store.log(team["id"], actor, action, slug, details)

    @staticmethod
    def _member_view(m: dict) -> dict:
        return {
            "handle": m["handle"],
            "display_name": m["display_name"],
            "role": m["role"],
            "created_at": m["created_at"],
            "has_token": bool(m["token_hash"]),
        }

    @staticmethod
    def _version_view(v: dict) -> dict:
        return {
            "version": v["version"],
            "author": v["author"],
            "approved_by": v["approved_by"],
            "note": v["note"],
            "content_hash": v["content_hash"],
            "created_at": v["created_at"],
        }

    @staticmethod
    def _review_view(r: dict) -> dict:
        out = {
            "id": r["id"],
            "submitted_by": r["submitted_by"],
            "bump": r["bump"],
            "note": r["note"],
            "content_hash": r["content_hash"],
            "created_at": r["created_at"],
            "decision": r["decision"],
            "decided_by": r["decided_by"],
            "reason": r["reason"],
            "decided_at": r["decided_at"],
        }
        if "skill_slug" in r:
            out["skill"] = r["skill_slug"]
            out["skill_title"] = r["skill_title"]
        return out

    def _status(self, skill: dict, pending: dict | None, channels: dict | None = None) -> str:
        if skill["lifecycle"] == "deprecated":
            return "deprecated"
        if pending is not None:
            return "in_review"
        channels = channels if channels is not None else self.store.channels(skill["id"])
        if DEFAULT_CHANNEL in channels:
            return "published"
        if skill["latest_version_id"]:
            return "approved"
        return "draft"

    @staticmethod
    def _channel_view(c: dict) -> dict:
        return {"version": c["version"], "content_hash": c["content_hash"], "set_by": c["set_by"], "set_at": c["set_at"]}

    def _checks_view(self, skill: dict) -> dict | None:
        run = self.store.latest_check_run(skill["id"])
        if run is None:
            return None
        draft_hash = skillfile.content_hash(skill["draft_content"]) if skill["draft_content"] is not None else None
        return {
            "passed": run["passed"],
            "failed": sum(1 for r in run["results"] if not r["passed"]),
            "total": len(run["results"]),
            "content_hash": run["content_hash"],
            "run_by": run["run_by"],
            "run_at": run["run_at"],
            "current": run["content_hash"] == draft_hash,
        }

    def _skill_view(self, skill: dict, install_count: int | None = None) -> dict:
        pending = self.store.pending_review(skill["id"])
        latest = self.store.version_by_id(skill["latest_version_id"]) if skill["latest_version_id"] else None
        channels = self.store.channels(skill["id"])
        view = {
            "slug": skill["slug"],
            "title": skill["title"],
            "description": skill["description"],
            "owner": skill["owner_handle"],
            "tags": json.loads(skill["tags"]),
            "status": self._status(skill, pending, channels),
            "lifecycle": skill["lifecycle"],
            "deprecation_reason": skill["deprecation_reason"],
            "latest_version": latest["version"] if latest else None,
            "latest_hash": latest["content_hash"] if latest else None,
            "approved_at": latest["created_at"] if latest else None,
            "channels": {name: self._channel_view(c) for name, c in channels.items()},
            "production_version": channels[DEFAULT_CHANNEL]["version"] if DEFAULT_CHANNEL in channels else None,
            "checks": self._checks_view(skill),
            "has_draft": skill["draft_content"] is not None,
            "draft_editor": skill["draft_editor"] if skill["draft_content"] is not None else None,
            "draft_updated_at": skill["draft_updated_at"] if skill["draft_content"] is not None else None,
            "pending_review": self._review_view(pending) if pending else None,
            "created_at": skill["created_at"],
            "updated_at": skill["updated_at"],
        }
        if install_count is not None:
            view["installs"] = install_count
        return view

    def _next_version(self, skill: dict, bump: str) -> str:
        if not skill["latest_version_id"]:
            return semver.INITIAL
        latest = self.store.version_by_id(skill["latest_version_id"])
        return semver.bump(latest["version"], bump)

    # ---------------------------------------------------------------- bootstrap
    def create_team(self, slug: str, name: str, owner_handle: str, owner_display: str | None = None) -> dict:
        """Create a team with its first owner. Returns the team and the owner's token."""
        if not SLUG_RE.match(slug or ""):
            raise Invalid("team slug must be lowercase letters, digits and single hyphens")
        if not HANDLE_RE.match(owner_handle or ""):
            raise Invalid("handle must be lowercase letters, digits, '.', '_' or '-' (max 64 chars)")
        if self.store.team_by_slug(slug):
            raise Conflict(f"team {slug!r} already exists")
        token = _new_token()
        with self.store.tx():
            team_id = self.store.insert_team(slug, name or slug)
            self.store.insert_member(team_id, owner_handle, owner_display or owner_handle, "owner", _hash_token(token))
            team = self.store.team_by_slug(slug)
            self._log(team, owner_handle, "team.created", None, name=team["name"])
        return {"team": {"slug": slug, "name": team["name"]}, "owner": owner_handle, "token": token}

    def list_teams(self) -> list[dict]:
        return [{"slug": t["slug"], "name": t["name"], "created_at": t["created_at"]} for t in self.store.teams()]

    def authenticate(self, token: str) -> tuple[str, str] | None:
        """Resolve a member token to ``(team_slug, handle)`` or None."""
        if not token or not token.startswith(TOKEN_PREFIX):
            return None
        m = self.store.member_by_token_hash(_hash_token(token))
        return (m["team_slug"], m["handle"]) if m else None

    # ----------------------------------------------------------- beta waitlist
    @staticmethod
    def _clean_signup(email, team_size="", tools=None, note="", source="") -> tuple[str, str, list[str], str, str]:
        email = str(email or "").strip().lower()
        if not EMAIL_RE.match(email) or len(email) > 254:
            raise Invalid("enter a valid email address")
        team_size = str(team_size or "").strip()
        team_size = team_size if team_size in BETA_TEAM_SIZES else ""
        if isinstance(tools, str):
            tools = [t for t in re.split(r"[,;\s]+", tools) if t]
        wanted = [str(t).strip().lower() for t in (tools or []) if str(t).strip()]
        tools = [t for t in BETA_TOOLS if t in wanted]
        note = str(note or "").strip()[:2000]
        source = str(source or "").strip()[:200]
        return email, team_size, tools, note, source

    def record_beta_signup(self, email: str, team_size: str = "", tools=None, note: str = "", source: str = "") -> dict:
        """Store a landing-page sign-up. Unauthenticated by design: validated,
        size-capped, restricted to the form's own choices, and merged without
        overwriting when the same email posts again."""
        email, team_size, tools, note, source = self._clean_signup(email, team_size, tools, note, source)
        new = self.store.upsert_beta_signup(email, team_size, tools, note, source)
        return {"email": email, "team_size": team_size, "tools": tools, "new": new}

    def _waitlist_admin(self, team: str, actor: str) -> None:
        t, m = self._ctx(team, actor)
        self._require(m, "owner", "reading or changing the beta waitlist")
        admin = self.waitlist_team
        if admin is None:
            teams = self.store.teams()
            if len(teams) != 1:
                raise Forbidden(
                    "this database holds several teams, so the waitlist has no default owner; "
                    "set SKILLCURRENT_WAITLIST_TEAM (or serve --waitlist-team) to the team whose owners may read it"
                )
            admin = teams[0]["slug"]
        if t["slug"] != admin:
            raise Forbidden(f"the waitlist belongs to team {admin!r}; owners of {t['slug']!r} cannot read it")

    @rpc
    def list_beta_signups(self, team: str, actor: str) -> list[dict]:
        self._waitlist_admin(team, actor)
        return [
            {k: r[k] for k in ("email", "team_size", "tools", "note", "source", "submissions", "created_at", "updated_at")}
            for r in self.store.beta_signups()
        ]

    @rpc
    def remove_beta_signup(self, team: str, actor: str, email: str) -> dict:
        """Delete one sign-up, for deletion requests. Returns whether a row existed."""
        self._waitlist_admin(team, actor)
        email = str(email or "").strip().lower()
        return {"email": email, "removed": self.store.delete_beta_signup(email)}

    @rpc
    def import_beta_signups(self, team: str, actor: str, rows: list) -> dict:
        """Merge sign-ups exported from elsewhere (for example a static host's
        form service) into the waitlist. Rows that fail validation are
        counted, never half-stored."""
        self._waitlist_admin(team, actor)
        if not isinstance(rows, list) or len(rows) > 5000:
            raise Invalid("rows must be a list of at most 5000 objects")
        added = merged = skipped = 0
        for row in rows:
            if not isinstance(row, dict):
                skipped += 1
                continue
            try:
                email, team_size, tools, note, source = self._clean_signup(
                    row.get("email"), row.get("team_size"), row.get("tools"), row.get("note"), row.get("source")
                )
            except Invalid:
                skipped += 1
                continue
            created = str(row.get("created_at") or "").strip() or None
            if self.store.upsert_beta_signup(email, team_size, tools, note, source, created_at=created):
                added += 1
            else:
                merged += 1
        return {"added": added, "merged": merged, "skipped": skipped}

    # ------------------------------------------------------------------ members
    @rpc
    def whoami(self, team: str, actor: str) -> dict:
        t, m = self._ctx(team, actor)
        return {"team": {"slug": t["slug"], "name": t["name"]}, "member": self._member_view(m)}

    @rpc
    def list_members(self, team: str, actor: str) -> list[dict]:
        t, _ = self._ctx(team, actor)
        return [self._member_view(m) for m in self.store.members(t["id"])]

    @rpc
    def add_member(self, team: str, actor: str, handle: str, role: str = "contributor", display_name: str | None = None) -> dict:
        t, m = self._ctx(team, actor)
        self._require(m, "owner", "adding members")
        if role not in ROLES:
            raise Invalid(f"role must be one of {', '.join(ROLES)}")
        if not HANDLE_RE.match(handle or ""):
            raise Invalid("handle must be lowercase letters, digits, '.', '_' or '-' (max 64 chars)")
        if self.store.member(t["id"], handle):
            raise Conflict(f"{handle!r} is already a member")
        token = _new_token()
        with self.store.tx():
            self.store.insert_member(t["id"], handle, display_name or handle, role, _hash_token(token))
            self._log(t, actor, "member.added", None, handle=handle, role=role)
        view = self._member_view(self.store.member(t["id"], handle))
        view["token"] = token
        return view

    @rpc
    def set_role(self, team: str, actor: str, handle: str, role: str) -> dict:
        t, m = self._ctx(team, actor)
        self._require(m, "owner", "changing roles")
        if role not in ROLES:
            raise Invalid(f"role must be one of {', '.join(ROLES)}")
        target = self._member(t, handle)
        if target["role"] == "owner" and role != "owner" and self._owner_count(t) <= 1:
            raise Conflict("a team must keep at least one owner")
        with self.store.tx():
            self.store.update_member(target["id"], role=role)
            self._log(t, actor, "member.role", None, handle=handle, role=role, previous=target["role"])
        return self._member_view(self.store.member(t["id"], handle))

    def _owner_count(self, team: dict) -> int:
        return sum(1 for x in self.store.members(team["id"]) if x["role"] == "owner")

    @rpc
    def remove_member(self, team: str, actor: str, handle: str) -> dict:
        t, m = self._ctx(team, actor)
        self._require(m, "owner", "removing members")
        target = self._member(t, handle)
        if target["role"] == "owner" and self._owner_count(t) <= 1:
            raise Conflict("a team must keep at least one owner")
        reassigned = []
        with self.store.tx():
            for skill in self.store.skills(t["id"]):
                if skill["owner_handle"] == handle:
                    self.store.update_skill(skill["id"], owner_handle=actor)
                    reassigned.append(skill["slug"])
            self.store.delete_member(target["id"])
            self._log(t, actor, "member.removed", None, handle=handle, reassigned_skills=reassigned)
        return {"removed": handle, "reassigned_skills": reassigned}

    @rpc
    def rotate_token(self, team: str, actor: str, handle: str | None = None) -> dict:
        t, m = self._ctx(team, actor)
        handle = handle or actor
        if handle != actor:
            self._require(m, "owner", "rotating another member's token")
        target = self._member(t, handle)
        token = _new_token()
        with self.store.tx():
            self.store.update_member(target["id"], token_hash=_hash_token(token))
            self._log(t, actor, "member.token_rotated", None, handle=handle)
        return {"handle": handle, "token": token}

    # ------------------------------------------------------------------- skills
    @rpc
    def validate(self, team: str, actor: str, content: str) -> dict:
        self._ctx(team, actor)
        problems, warnings, doc = skillfile.check(content)
        return {"ok": not problems, "problems": problems, "warnings": warnings, "name": doc.name if doc else None}

    @rpc
    def create_skill(self, team: str, actor: str, content: str) -> dict:
        t, m = self._ctx(team, actor)
        self._require(m, "contributor", "creating skills")
        doc = skillfile.parse(content)
        if self.store.skill(t["id"], doc.name):
            raise Conflict(f"skill {doc.name!r} already exists; edit its draft instead")
        with self.store.tx():
            self.store.insert_skill(t["id"], doc.name, doc.title, doc.description, actor, doc.tags, content, actor)
            self._log(t, actor, "skill.created", doc.name)
        return self._skill_view(self._skill(t, doc.name))

    @rpc
    def update_draft(self, team: str, actor: str, slug: str, content: str) -> dict:
        t, m = self._ctx(team, actor)
        skill = self._skill(t, slug)
        self._require_edit(m, skill)
        if self.store.pending_review(skill["id"]):
            raise Conflict(f"{slug!r} is in review; withdraw the review (or wait for a decision) before editing")
        doc = skillfile.parse(content)
        if doc.name != slug:
            raise Invalid(f"front matter name {doc.name!r} must match the skill slug {slug!r}")
        if skill["draft_content"] == content:
            return self._skill_view(skill)
        with self.store.tx():
            self.store.update_skill(
                skill["id"], draft_content=content, draft_editor=actor, draft_updated_at=now(),
                title=doc.title, description=doc.description, tags=doc.tags,
            )
            self._log(t, actor, "skill.draft_updated", slug)
        return self._skill_view(self._skill(t, slug))

    @rpc
    def discard_draft(self, team: str, actor: str, slug: str) -> dict:
        t, m = self._ctx(team, actor)
        skill = self._skill(t, slug)
        self._require_edit(m, skill)
        if skill["draft_content"] is None:
            raise Invalid(f"{slug!r} has no draft")
        if self.store.pending_review(skill["id"]):
            raise Conflict(f"{slug!r} is in review; withdraw the review before discarding the draft")
        if not skill["latest_version_id"]:
            raise Conflict(f"{slug!r} has never been published; use delete_skill to remove it entirely")
        latest = self.store.version_by_id(skill["latest_version_id"])
        doc = skillfile.parse(latest["content"])
        with self.store.tx():
            self.store.update_skill(
                skill["id"], draft_content=None, draft_editor=None, draft_updated_at=None,
                title=doc.title, description=doc.description, tags=doc.tags,
            )
            self._log(t, actor, "skill.draft_discarded", slug)
        return self._skill_view(self._skill(t, slug))

    @rpc
    def delete_skill(self, team: str, actor: str, slug: str) -> dict:
        t, m = self._ctx(team, actor)
        self._require(m, "maintainer", "deleting skills")
        skill = self._skill(t, slug)
        if skill["latest_version_id"]:
            raise Conflict(f"{slug!r} has published versions; deprecate it instead of deleting it")
        with self.store.tx():
            self.store.delete_skill(skill["id"])
            self._log(t, actor, "skill.deleted", slug)
        return {"deleted": slug}

    @rpc
    def set_owner(self, team: str, actor: str, slug: str, handle: str) -> dict:
        t, m = self._ctx(team, actor)
        self._require(m, "maintainer", "transferring skill ownership")
        skill = self._skill(t, slug)
        target = self._member(t, handle)
        if RANK[target["role"]] < RANK["contributor"]:
            raise Invalid(f"{handle!r} is a viewer and cannot own skills")
        with self.store.tx():
            self.store.update_skill(skill["id"], owner_handle=handle)
            self._log(t, actor, "skill.owner_changed", slug, owner=handle, previous=skill["owner_handle"])
        return self._skill_view(self._skill(t, slug))

    @rpc
    def list_skills(self, team: str, actor: str, status: str | None = None, tag: str | None = None, query: str | None = None) -> list[dict]:
        t, _ = self._ctx(team, actor)
        if status is not None and status not in STATUSES:
            raise Invalid(f"status must be one of {', '.join(STATUSES)}")
        counts = self.store.install_counts(t["id"])
        views = []
        q = (query or "").strip().lower()
        for skill in self.store.skills(t["id"]):
            view = self._skill_view(skill, counts.get(skill["id"], 0))
            if status and view["status"] != status:
                continue
            if tag and tag.lower() not in view["tags"]:
                continue
            if q and q not in f"{view['slug']} {view['title']} {view['description']} {' '.join(view['tags'])}".lower():
                continue
            views.append(view)
        return views

    @rpc
    def get_skill(self, team: str, actor: str, slug: str) -> dict:
        t, _ = self._ctx(team, actor)
        skill = self._skill(t, slug)
        view = self._skill_view(skill, self.store.install_counts(t["id"]).get(skill["id"], 0))
        view["versions"] = [self._version_view(v) for v in self.store.versions(skill["id"])]
        view["reviews"] = [self._review_view(r) for r in self.store.reviews(skill["id"])]
        return view

    def _resolve_ref(self, skill: dict, ref: str | None) -> tuple[str, str, str]:
        """Return (ref label, version string, content) for ``ref``.

        ``ref`` is None/"production" (the production channel), "canary",
        "latest" (newest approved version), "draft", or a version number.
        """
        if ref in (None, "", *CHANNELS):
            channel = ref or DEFAULT_CHANNEL
            c = self.store.channel(skill["id"], channel)
            if c is None:
                hint = "use ref='latest' for the newest approved version or ref='draft'" if skill["latest_version_id"] else "use ref='draft'"
                raise NotFound(f"{skill['slug']!r} has no release on the {channel} channel ({hint})")
            v = self.store.version_by_id(c["version_id"])
            return channel, v["version"], v["content"]
        if ref == "latest":
            if not skill["latest_version_id"]:
                raise NotFound(f"{skill['slug']!r} has no approved version yet (use ref='draft')")
            v = self.store.version_by_id(skill["latest_version_id"])
            return "latest", v["version"], v["content"]
        if ref == "draft":
            if skill["draft_content"] is None:
                raise NotFound(f"{skill['slug']!r} has no draft")
            return "draft", "draft", skill["draft_content"]
        v = self.store.version(skill["id"], ref)
        if v is None:
            raise NotFound(f"{skill['slug']!r} has no version {ref!r}")
        return ref, v["version"], v["content"]

    @rpc
    def get_content(self, team: str, actor: str, slug: str, ref: str | None = None) -> dict:
        t, _ = self._ctx(team, actor)
        skill = self._skill(t, slug)
        label, version, content = self._resolve_ref(skill, ref)
        return {
            "slug": slug,
            "ref": label,
            "channel": label if label in CHANNELS else None,
            "version": version,
            "content": content,
            "content_hash": skillfile.content_hash(content),
            "lifecycle": skill["lifecycle"],
        }

    @rpc
    def list_versions(self, team: str, actor: str, slug: str) -> list[dict]:
        t, _ = self._ctx(team, actor)
        skill = self._skill(t, slug)
        return [self._version_view(v) for v in self.store.versions(skill["id"])]

    @rpc
    def diff(self, team: str, actor: str, slug: str, from_ref: str | None = "latest", to_ref: str | None = "draft") -> dict:
        t, _ = self._ctx(team, actor)
        skill = self._skill(t, slug)
        try:
            from_label, _, a = self._resolve_ref(skill, from_ref or "latest")
        except NotFound:
            if from_ref in (None, "", "latest", *CHANNELS):
                from_label, a = "empty", ""
            else:
                raise
        to_label, _, b = self._resolve_ref(skill, to_ref)
        lines = difflib.unified_diff(
            a.splitlines(keepends=True), b.splitlines(keepends=True),
            fromfile=f"{slug}@{from_label}", tofile=f"{slug}@{to_label}",
        )
        return {"slug": slug, "from": from_label, "to": to_label, "diff": "".join(lines)}

    # ------------------------------------------------------------------- review
    @rpc
    def submit_review(self, team: str, actor: str, slug: str, bump: str = "minor", note: str = "") -> dict:
        t, m = self._ctx(team, actor)
        skill = self._skill(t, slug)
        self._require_edit(m, skill)
        if bump not in semver.BUMPS:
            raise Invalid(f"bump must be one of {', '.join(semver.BUMPS)}")
        if skill["draft_content"] is None:
            raise Invalid(f"{slug!r} has no draft to submit")
        if self.store.pending_review(skill["id"]):
            raise Conflict(f"{slug!r} already has a pending review")
        skillfile.parse(skill["draft_content"])  # re-validate; fail loudly if the draft went stale
        content_hash = skillfile.content_hash(skill["draft_content"])
        run = self.store.latest_check_run(skill["id"])
        if run is None or run["content_hash"] != content_hash:
            raise Conflict(f"run the checks on the current draft of {slug!r} before submitting it (run_checks)")
        if not run["passed"]:
            raise Conflict(f"the current draft of {slug!r} fails {sum(1 for r in run['results'] if not r['passed'])} check(s); fix them and rerun")
        with self.store.tx():
            review_id = self.store.insert_review(skill["id"], actor, bump, note or "", content_hash)
            self._log(t, actor, "review.submitted", slug, bump=bump, note=note or "", proposed_version=self._next_version(skill, bump))
        review = self._review_view(self.store.one("SELECT * FROM reviews WHERE id = ?", (review_id,)))
        review["skill"] = slug
        review["proposed_version"] = self._next_version(skill, bump)
        return review

    @rpc
    def withdraw_review(self, team: str, actor: str, slug: str) -> dict:
        t, m = self._ctx(team, actor)
        skill = self._skill(t, slug)
        pending = self.store.pending_review(skill["id"])
        if pending is None:
            raise Invalid(f"{slug!r} has no pending review")
        if pending["submitted_by"] != actor:
            self._require(m, "maintainer", "withdrawing someone else's review")
        with self.store.tx():
            self.store.decide_review(pending["id"], "withdrawn", actor, "")
            self._log(t, actor, "review.withdrawn", slug)
        return self._skill_view(self._skill(t, slug))

    @rpc
    def approve(self, team: str, actor: str, slug: str, comment: str = "", release: str | None = None) -> dict:
        """Freeze the submitted draft as an immutable, approved version.

        Approval attaches to these exact bytes and nothing is installed until
        the version is released to a channel; ``release`` performs that step
        in the same call for teams that do not stage rollouts.
        """
        t, m = self._ctx(team, actor)
        self._require(m, "maintainer", "approving skills")
        if release is not None and release not in CHANNELS:
            raise Invalid(f"release must be one of {', '.join(CHANNELS)}")
        skill = self._skill(t, slug)
        pending = self.store.pending_review(skill["id"])
        if pending is None:
            raise Invalid(f"{slug!r} has no pending review")
        if pending["submitted_by"] == actor:
            raise Forbidden("a submission must be approved by a different maintainer")
        if actor in self.store.draft_authors(t["id"], slug):
            # Separation of duties covers editors too: approving bytes you wrote is approving your own work.
            raise Forbidden("you wrote or edited this draft; it must be approved by a maintainer who did neither")
        draft = skill["draft_content"]
        if draft is None or skillfile.content_hash(draft) != pending["content_hash"]:
            raise Conflict("the draft changed since it was submitted; ask for a fresh submission")
        version = self._next_version(skill, pending["bump"])
        content = skillfile.stamp_version(draft, version)
        doc = skillfile.parse(content)
        with self.store.tx():
            version_id = self.store.insert_version(
                skill["id"], version, content, skillfile.content_hash(content), pending["submitted_by"], actor, pending["note"]
            )
            self.store.decide_review(pending["id"], "approved", actor, comment or "")
            self.store.update_skill(
                skill["id"], latest_version_id=version_id, draft_content=None, draft_editor=None, draft_updated_at=None,
                lifecycle="active", deprecation_reason=None, title=doc.title, description=doc.description, tags=doc.tags,
            )
            self._log(t, actor, "skill.approved", slug, version=version, author=pending["submitted_by"], comment=comment or "")
            if release:
                self._set_channel(t, actor, self._skill(t, slug), version, release, "release", pending["note"])
        out = self._skill_view(self._skill(t, slug))
        out["approved"] = self._version_view(self.store.version_by_id(version_id))
        return out

    def _set_channel(self, team: dict, actor: str, skill: dict, version: str, channel: str, kind: str, reason: str) -> dict:
        v = self.store.version(skill["id"], version)
        if v is None:
            raise NotFound(f"{skill['slug']!r} has no approved version {version!r}")
        current = self.store.channel(skill["id"], channel)
        previous = current["version"] if current else None
        if previous == version:
            raise Conflict(f"{skill['slug']!r} {version} is already the {channel} release")
        with self.store.tx():
            self.store.set_channel(skill["id"], channel, v["id"], version, previous, kind, reason or "", actor)
            self._log(team, actor, f"skill.{kind}", skill["slug"], channel=channel, version=version, previous=previous, reason=reason or "")
        return {"slug": skill["slug"], "channel": channel, "version": version, "previous": previous, "content_hash": v["content_hash"]}

    @rpc
    def release(self, team: str, actor: str, slug: str, version: str | None = None, channel: str = DEFAULT_CHANNEL, note: str = "") -> dict:
        """Point a channel at an approved version. Installs on that channel now resolve to these bytes."""
        t, m = self._ctx(team, actor)
        self._require(m, "maintainer", "releasing skills")
        if channel not in CHANNELS:
            raise Invalid(f"channel must be one of {', '.join(CHANNELS)}")
        skill = self._skill(t, slug)
        if skill["lifecycle"] == "deprecated":
            raise Conflict(f"{slug!r} is deprecated; restore it before releasing")
        if version is None:
            if not skill["latest_version_id"]:
                raise Invalid(f"{slug!r} has no approved version to release")
            version = self.store.version_by_id(skill["latest_version_id"])["version"]
        return self._set_channel(t, actor, skill, version, channel, "release", note)

    @rpc
    def rollback(self, team: str, actor: str, slug: str, channel: str = DEFAULT_CHANNEL, reason: str = "") -> dict:
        """Point a channel back at the version it served before the current one.

        Rollback changes what the channel resolves to. It does not delete
        downloaded copies; ``sync`` brings environments back in line.
        """
        t, m = self._ctx(team, actor)
        self._require(m, "maintainer", "rolling back releases")
        if channel not in CHANNELS:
            raise Invalid(f"channel must be one of {', '.join(CHANNELS)}")
        skill = self._skill(t, slug)
        current = self.store.channel(skill["id"], channel)
        if current is None:
            raise Invalid(f"{slug!r} has nothing on the {channel} channel")
        previous = next((h for h in self.store.channel_history(skill["id"], channel) if h["version"] != current["version"]), None)
        if previous is None:
            raise Invalid(f"{slug!r} has no earlier {channel} release to roll back to")
        return self._set_channel(t, actor, skill, previous["version"], channel, "rollback", reason)

    @rpc
    def release_history(self, team: str, actor: str, slug: str) -> dict:
        t, _ = self._ctx(team, actor)
        skill = self._skill(t, slug)
        channels = self.store.channels(skill["id"])
        return {
            "slug": slug,
            "channels": {name: self._channel_view(c) for name, c in channels.items()},
            "versions": [self._version_view(v) for v in self.store.versions(skill["id"])],
            "events": [
                {
                    "channel": h["channel"], "version": h["version"], "previous_version": h["previous_version"],
                    "kind": h["kind"], "reason": h["reason"], "by": h["set_by"], "at": h["set_at"],
                }
                for h in self.store.channel_history(skill["id"])
            ],
        }

    @rpc
    def passport(self, team: str, actor: str, slug: str) -> dict:
        """Identity, ownership and boundaries that travel with the skill."""
        t, _ = self._ctx(team, actor)
        skill = self._skill(t, slug)
        view = self._skill_view(skill, self.store.install_counts(t["id"]).get(skill["id"], 0))
        adoption = self.adoption(team, actor, slug=slug)
        return {
            "canonical_id": f"{t['slug']}/{slug}",
            "slug": slug,
            "title": view["title"],
            "description": view["description"],
            "owner": view["owner"],
            "tags": view["tags"],
            "status": view["status"],
            "lifecycle": view["lifecycle"],
            "latest_version": view["latest_version"],
            "latest_hash": view["latest_hash"],
            "channels": view["channels"],
            "rules": [r["name"] for r in self.store.rules(t["id"])],
            "environments": adoption["summary"],
            "created_at": view["created_at"],
            "updated_at": view["updated_at"],
        }

    # ------------------------------------------------------------------- checks
    @rpc
    def check_content(self, team: str, actor: str, content: str) -> dict:
        """Run the checks against arbitrary content without recording anything."""
        t, _ = self._ctx(team, actor)
        return checks.run(content, self.store.rules(t["id"]))

    @rpc
    def run_checks(self, team: str, actor: str, slug: str) -> dict:
        """Run the checks against the current draft and record the result.

        Submission requires a passing run against the exact draft bytes.
        """
        t, m = self._ctx(team, actor)
        skill = self._skill(t, slug)
        self._require_edit(m, skill)
        if skill["draft_content"] is None:
            raise Invalid(f"{slug!r} has no draft to check")
        report = checks.run(skill["draft_content"], self.store.rules(t["id"]))
        with self.store.tx():
            self.store.insert_check_run(skill["id"], report["content_hash"], report["passed"], report["results"], actor)
            self._log(t, actor, "checks.run", slug, passed=report["passed"], failed=report["failed"], total=report["total"])
        report["slug"] = slug
        report["run_by"] = actor
        return report

    @rpc
    def list_rules(self, team: str, actor: str) -> list[dict]:
        t, _ = self._ctx(team, actor)
        return [self._rule_view(r) for r in self.store.rules(t["id"])]

    @staticmethod
    def _rule_view(r: dict) -> dict:
        return {k: r[k] for k in ("name", "kind", "pattern", "category", "rationale", "created_by", "created_at")}

    @rpc
    def add_rule(self, team: str, actor: str, name: str, kind: str, pattern: str, category: str = "team", rationale: str = "") -> dict:
        t, m = self._ctx(team, actor)
        self._require(m, "maintainer", "adding check rules")
        if not SLUG_RE.match(name or ""):
            raise Invalid("rule name must be lowercase letters, digits and single hyphens")
        if kind not in checks.RULE_KINDS:
            raise Invalid(f"kind must be one of {', '.join(checks.RULE_KINDS)}")
        if category not in checks.CATEGORIES:
            raise Invalid(f"category must be one of {', '.join(checks.CATEGORIES)}")
        try:
            re.compile(pattern or "")
        except re.error as exc:
            raise Invalid(f"pattern is not a valid regular expression: {exc}") from None
        if not pattern:
            raise Invalid("pattern is required")
        if self.store.rule(t["id"], name):
            raise Conflict(f"rule {name!r} already exists")
        with self.store.tx():
            self.store.insert_rule(t["id"], name, kind, pattern, category, rationale or "", actor)
            self._log(t, actor, "rule.added", None, name=name, kind=kind, pattern=pattern)
        return self._rule_view(self.store.rule(t["id"], name))

    @rpc
    def remove_rule(self, team: str, actor: str, name: str) -> dict:
        t, m = self._ctx(team, actor)
        self._require(m, "maintainer", "removing check rules")
        rule = self.store.rule(t["id"], name)
        if rule is None:
            raise NotFound(f"rule {name!r} does not exist")
        with self.store.tx():
            self.store.delete_rule(rule["id"])
            self._log(t, actor, "rule.removed", None, name=name)
        return {"removed": name}

    @rpc
    def reject(self, team: str, actor: str, slug: str, reason: str) -> dict:
        t, m = self._ctx(team, actor)
        self._require(m, "maintainer", "rejecting skills")
        skill = self._skill(t, slug)
        pending = self.store.pending_review(skill["id"])
        if pending is None:
            raise Invalid(f"{slug!r} has no pending review")
        if not (reason or "").strip():
            raise Invalid("a rejection needs a reason the author can act on")
        with self.store.tx():
            self.store.decide_review(pending["id"], "rejected", actor, reason.strip())
            self._log(t, actor, "review.rejected", slug, reason=reason.strip(), author=pending["submitted_by"])
        return self._skill_view(self._skill(t, slug))

    @rpc
    def pending_reviews(self, team: str, actor: str) -> list[dict]:
        t, _ = self._ctx(team, actor)
        out = []
        for r in self.store.pending_reviews(t["id"]):
            view = self._review_view(r)
            view["proposed_version"] = self._next_version(self.store.skill_by_id(r["skill_id"]), r["bump"])
            out.append(view)
        return out

    @rpc
    def deprecate(self, team: str, actor: str, slug: str, reason: str = "") -> dict:
        t, m = self._ctx(team, actor)
        self._require(m, "maintainer", "deprecating skills")
        skill = self._skill(t, slug)
        if not skill["latest_version_id"]:
            raise Invalid(f"{slug!r} was never published; delete it instead")
        if skill["lifecycle"] == "deprecated":
            raise Conflict(f"{slug!r} is already deprecated")
        with self.store.tx():
            self.store.update_skill(skill["id"], lifecycle="deprecated", deprecation_reason=reason or "")
            self._log(t, actor, "skill.deprecated", slug, reason=reason or "")
        return self._skill_view(self._skill(t, slug))

    @rpc
    def restore(self, team: str, actor: str, slug: str) -> dict:
        t, m = self._ctx(team, actor)
        self._require(m, "maintainer", "restoring skills")
        skill = self._skill(t, slug)
        if skill["lifecycle"] != "deprecated":
            raise Conflict(f"{slug!r} is not deprecated")
        with self.store.tx():
            self.store.update_skill(skill["id"], lifecycle="active", deprecation_reason=None)
            self._log(t, actor, "skill.restored", slug)
        return self._skill_view(self._skill(t, slug))

    # ----------------------------------------------------------------- installs
    @rpc
    def record_install(self, team: str, actor: str, slug: str, version: str, target: str, path: str, content_hash: str, host: str = "", channel: str = DEFAULT_CHANNEL, reason: str = "") -> dict:
        """Record that an environment (member + host + target) holds these bytes, and write an ``installed`` receipt."""
        t, _ = self._ctx(team, actor)
        skill = self._skill(t, slug)
        if version != "draft" and self.store.version(skill["id"], version) is None:
            raise NotFound(f"{slug!r} has no version {version!r}")
        if channel not in CHANNELS:
            raise Invalid(f"channel must be one of {', '.join(CHANNELS)}")
        host = host or ""
        with self.store.tx():
            self.store.upsert_install(t["id"], actor, host, skill["id"], version, channel, target, path, content_hash)
            self.store.insert_receipt(t["id"], actor, host, target, skill["id"], version, "installed", content_hash, path)
            self._log(t, actor, "skill.installed", slug, version=version, target=target, host=host, channel=channel, reason=str(reason or "")[:60])
        return {"slug": slug, "version": version, "target": target, "path": path, "host": host, "channel": channel}

    @rpc
    def remove_install(self, team: str, actor: str, slug: str, target: str, host: str = "") -> dict:
        t, _ = self._ctx(team, actor)
        skill = self._skill(t, slug)
        with self.store.tx():
            removed = self.store.delete_install(t["id"], actor, host or "", skill["id"], target)
            if removed:
                self._log(t, actor, "skill.uninstalled", slug, target=target, host=host or "")
        return {"slug": slug, "target": target, "host": host or "", "removed": bool(removed)}

    @rpc
    def report(self, team: str, actor: str, slug: str, event: str, target: str, host: str = "", version: str = "", content_hash: str = "", detail: str = "") -> dict:
        """Record a receipt from an environment: ``verified`` (bytes re-hashed and
        matched), ``loaded`` (a runtime reported loading the skill) or
        ``task_tested`` (a named fixture task passed). Receipts are evidence
        about a specific event, never proof that an agent always follows a skill."""
        t, _ = self._ctx(team, actor)
        skill = self._skill(t, slug)
        if event not in RECEIPT_EVENTS[1:]:
            raise Invalid(f"event must be one of {', '.join(RECEIPT_EVENTS[1:])}")
        host = host or ""
        if not version:
            install = next(iter(self.store.installs(t["id"], actor, host, skill["id"])), None)
            install = install if install and install["target"] == target else next((i for i in self.store.installs(t["id"], actor, host, skill["id"]) if i["target"] == target), None)
            if install is None:
                raise Invalid(f"{slug!r} is not recorded as installed for {actor}@{host or 'default'} on {target}; pass version explicitly")
            version = install["version"]
            content_hash = content_hash or install["content_hash"]
        with self.store.tx():
            self.store.insert_receipt(t["id"], actor, host, target, skill["id"], version, event, content_hash or "", detail or "")
            # `verified` fires on every status/sync run; it lives in receipts only so
            # it cannot bury review and drift events in the activity log.
            if event != "verified":
                self._log(t, actor, f"receipt.{event}", slug, version=version, target=target, host=host, detail=detail or "")
        return {"slug": slug, "event": event, "version": version, "target": target, "host": host}

    @rpc
    def report_drift(self, team: str, actor: str, slug: str, target: str, state: str, host: str = "", installed_version: str = "",
                     channel_version: str = "", response: str = "observed") -> dict:
        """Record that an environment's copy did not match what its channel serves.

        ``state`` is what ``status`` found (modified, missing, outdated);
        ``response`` is what happened next (observed by status, repaired or
        forced by sync, or kept_local because the member has local edits).
        Written to the activity log only, and de-duplicated within an episode
        so a hook that runs every session does not repeat the same finding.
        This is the evidence for the beta question "did sync ever catch a bad copy"."""
        t, _ = self._ctx(team, actor)
        self._skill(t, slug)
        if state not in DRIFT_STATES:
            raise Invalid(f"state must be one of {', '.join(DRIFT_STATES)}")
        if response not in DRIFT_RESPONSES:
            raise Invalid(f"response must be one of {', '.join(DRIFT_RESPONSES)}")
        host = host or ""
        details = {"target": target, "host": host, "installed": installed_version or "", "channel_version": channel_version or "", "response": response}
        for prev in self.store.activity(t["id"], 200, slug, action_prefix="drift."):
            d = prev["details"]
            if prev["actor"] != actor or d.get("host") != host or d.get("target") != target:
                continue
            if d.get("response") in ("repaired", "forced"):
                break  # the previous episode ended; this is a new one
            if prev["action"] == f"drift.{state}" and d.get("installed") == details["installed"] and d.get("response") == response:
                return {"slug": slug, "state": state, "response": response, "recorded": False}
        with self.store.tx():
            self._log(t, actor, f"drift.{state}", slug, **details)
        return {"slug": slug, "state": state, "response": response, "recorded": True}

    def _install_rows(self, team: dict, rows: list[dict]) -> list[dict]:
        skills = {s["id"]: s for s in self.store.skills(team["id"])}
        out = []
        for r in rows:
            skill = skills[r["skill_id"]]
            channel = self.store.channel(skill["id"], r["channel"])
            latest = self.store.version_by_id(skill["latest_version_id"]) if skill["latest_version_id"] else None
            out.append(
                {
                    "handle": r["handle"],
                    "host": r["host"],
                    "slug": r["skill_slug"],
                    "version": r["version"],
                    "channel": r["channel"],
                    "target": r["target"],
                    "path": r["path"],
                    "content_hash": r["content_hash"],
                    "installed_at": r["installed_at"],
                    "target_version": channel["version"] if channel else None,
                    "target_hash": channel["content_hash"] if channel else None,
                    "latest_version": latest["version"] if latest else None,
                    "lifecycle": skill["lifecycle"],
                }
            )
        return out

    @rpc
    def list_installs(self, team: str, actor: str, handle: str | None = None, host: str | None = None) -> list[dict]:
        """Installs for one member (own by default; ``handle='*'`` for everyone, maintainers only)."""
        t, m = self._ctx(team, actor)
        if handle == "*":
            self._require(m, "maintainer", "listing every member's installs")
            rows = self.store.installs(t["id"], None, host)
        else:
            handle = handle or actor
            if handle != actor:
                self._require(m, "maintainer", "listing another member's installs")
            rows = self.store.installs(t["id"], handle, host)
        return self._install_rows(t, rows)

    @rpc
    def adoption(self, team: str, actor: str, slug: str | None = None) -> dict:
        """Where each release landed: one row per environment with its strongest receipt.

        Evidence levels, weakest to strongest: installed (bytes written),
        verified (re-hashed and matched), loaded (a runtime reported loading
        it), task_tested (a named fixture task passed).
        """
        t, _ = self._ctx(team, actor)
        skill = self._skill(t, slug) if slug else None
        rows = self._install_rows(t, self.store.installs(t["id"], None, None, skill["id"] if skill else None))
        receipts = self.store.receipts(t["id"], skill["id"] if skill else None, limit=5000)
        best: dict[tuple, dict] = {}
        for r in receipts:
            key = (r["handle"], r["host"], r["target"], r["skill_slug"], r["version"])
            cur = best.get(key)
            if cur is None or EVIDENCE_RANK[r["event"]] > EVIDENCE_RANK[cur["event"]]:
                best[key] = r
        last_seen: dict[tuple, str] = {}
        for r in receipts:
            key = (r["handle"], r["host"], r["target"], r["skill_slug"])
            last_seen[key] = max(last_seen.get(key, ""), r["created_at"])
        environments = []
        for row in rows:
            key = (row["handle"], row["host"], row["target"], row["slug"], row["version"])
            strongest = best.get(key)
            evidence = strongest["event"] if strongest else "installed"
            if row["lifecycle"] == "deprecated":
                state = "deprecated"
            elif row["target_version"] is None:
                state = "unreleased"
            elif row["version"] == row["target_version"]:
                state = "current"
            else:
                state = "stale"
            environments.append(
                {
                    "environment": f"{row['handle']}@{row['host']}" if row["host"] else row["handle"],
                    "handle": row["handle"], "host": row["host"], "runtime": row["target"], "slug": row["slug"],
                    "version": row["version"], "channel": row["channel"], "target_version": row["target_version"],
                    "evidence": evidence, "state": state,
                    "last_seen": last_seen.get(key[:4], row["installed_at"]),
                    "needs_attention": state in ("stale", "deprecated"),
                }
            )
        environments.sort(key=lambda e: (-EVIDENCE_RANK[e["evidence"]], e["environment"], e["slug"]))
        summary = {
            "environments": len(environments),
            "current": sum(1 for e in environments if e["state"] == "current"),
            "needs_attention": sum(1 for e in environments if e["needs_attention"]),
        }
        for event in RECEIPT_EVENTS:
            summary[event] = sum(1 for e in environments if EVIDENCE_RANK[e["evidence"]] >= EVIDENCE_RANK[event])
        return {"slug": slug, "summary": summary, "environments": environments}

    @rpc
    def catalog_index(self, team: str, actor: str) -> dict:
        """A signed-off view of what is published: one entry per skill with its
        version and content digest, plus a digest of the whole index so a
        runtime can prove it retrieved the same generation as another."""
        t, _ = self._ctx(team, actor)
        entries = []
        for skill in self.store.skills(t["id"]):
            for name, c in self.store.channels(skill["id"]).items():
                entries.append(
                    {
                        "slug": skill["slug"], "channel": name, "version": c["version"], "content_hash": c["content_hash"],
                        "published_at": c["published_at"], "released_at": c["set_at"], "lifecycle": skill["lifecycle"],
                    }
                )
        canonical = json.dumps(entries, sort_keys=True, separators=(",", ":"))
        index_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        latest = max((e["published_at"] for e in entries), default=None)
        return {
            "team": t["slug"],
            "generation": f"{latest or 'empty'}-{index_hash[:12]}",
            "index_hash": index_hash,
            "generated_at": now(),
            "skills": entries,
        }

    # ---------------------------------------------------------- activity, stats
    @rpc
    def activity(self, team: str, actor: str, limit: int = 50, slug: str | None = None, action: str | None = None) -> list[dict]:
        t, _ = self._ctx(team, actor)
        limit = max(1, min(int(limit), 500))
        return [
            {"actor": a["actor"], "action": a["action"], "skill": a["skill_slug"], "details": a["details"], "at": a["created_at"]}
            for a in self.store.activity(t["id"], limit, slug, action_prefix=action or None)
        ]

    @rpc
    def beta_report(self, team: str, actor: str, days: int | None = None) -> dict:
        """Counts that answer the two beta questions, for a team to send back.

        Contains numbers only: no skill content, no member names, no emails.
        Reads the tables directly, so it is not limited by the activity cap.

        Review is mandatory in SkillCurrent (a submission needs a passing
        check run on its exact bytes and a different approver), so "kept
        review switched on" means the team kept using it instead of routing
        around it: approvals keep happening, drafts are not installed
        directly, reviews sometimes say no, and the server runs with tokens.
        """
        t, m = self._ctx(team, actor)
        self._require(m, "maintainer", "producing the beta report")
        since = None
        if days is not None:
            days = int(days)
            if days < 1 or days > 3650:
                raise Invalid("days must be between 1 and 3650")
            since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds").replace("+00:00", "Z")

        def ts(value: str | None) -> datetime | None:
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
            except ValueError:
                return None

        reviews = self.store.reviews_since(t["id"], since)
        decided = {d: [r for r in reviews if r["decision"] == d] for d in ("approved", "rejected", "withdrawn")}
        latencies = []
        for r in decided["approved"]:
            a, b = ts(r["created_at"]), ts(r["decided_at"])
            if a and b:
                latencies.append(max(0.0, (b - a).total_seconds()))
        acts = self.store.activity(t["id"], None, since=since)
        count = lambda prefix: sum(1 for a in acts if a["action"] == prefix)
        draft_installs = sum(1 for a in acts if a["action"] == "skill.installed" and a["details"].get("version") == "draft")
        drift = [a for a in acts if a["action"].startswith("drift.")]
        # Each finding is logged once when status sees it ("observed"), then once more for what sync did about it.
        findings = [a for a in drift if a["details"].get("response") == "observed"]
        by_state = {s: sum(1 for a in findings if a["action"] == f"drift.{s}") for s in DRIFT_STATES}
        by_response = {r: sum(1 for a in drift if a["details"].get("response") == r) for r in DRIFT_RESPONSES}
        drift_envs = {(a["actor"], a["details"].get("host"), a["details"].get("target"), a["skill_slug"]) for a in drift}
        # Did status/sync run at all? An intact copy leaves a `verified` receipt on every run; a bad one leaves a drift record.
        # Without this, "no drift" and "the hook never ran" look the same.
        verified = self.store.receipts_since(t["id"], "verified", since)
        check_marks = [(r["handle"], r["host"], r["target"], r["created_at"]) for r in verified]
        check_marks += [(a["actor"], a["details"].get("host", ""), a["details"].get("target", ""), a["created_at"]) for a in drift]
        checks = {
            "verified_receipts": len(verified),
            "environments_checked": len({m[:3] for m in check_marks}),
            "days_with_checks": len({m[3][:10] for m in check_marks}),
            "last_check_at": max((m[3] for m in check_marks), default=None),
        }
        adoption = self.adoption(team, actor)["summary"]

        review = {
            "submitted": len(reviews),
            "approved": len(decided["approved"]),
            "rejected": len(decided["rejected"]),
            "withdrawn": len(decided["withdrawn"]),
            "pending": sum(1 for r in reviews if r["decision"] is None),
            "distinct_approvers": len({r["decided_by"] for r in decided["approved"]}),
            "distinct_submitters": len({r["submitted_by"] for r in reviews}),
            "median_seconds_submit_to_approve": round(statistics.median(latencies)) if latencies else None,
            "approvals_under_60_seconds": sum(1 for x in latencies if x < 60),
            "releases": count("skill.release"),
            "rollbacks": count("skill.rollback"),
            "draft_installs": draft_installs,
        }
        bad = {s: by_state[s] for s in BAD_COPY_STATES}
        sync = {
            "checks": checks,
            "findings": len(findings),
            "bad_copies": sum(bad.values()),
            "by_state": by_state,
            "by_response": by_response,
            "environments_with_drift": len(drift_envs),
        }

        reasons = []
        if review["submitted"] == 0:
            review_verdict = "no activity"
            reasons.append("no submissions in the window")
        else:
            reasons.append(f"{review['approved']} approved, {review['rejected']} rejected, {review['withdrawn']} withdrawn of {review['submitted']} submitted")
            reasons.append(f"{review['distinct_approvers']} distinct approver(s)")
            if draft_installs:
                reasons.append(f"{draft_installs} install(s) of an unreviewed draft")
            if self.auth_mode == "none":
                reasons.append("server runs with --no-auth, so one person can act as author and approver")
            # A review that is always instant and never says no looks like a rubber stamp.
            rubber_stamp = bool(review["approved"]) and review["approvals_under_60_seconds"] * 2 > review["approved"] and not (review["rejected"] or review["withdrawn"])
            if rubber_stamp:
                reasons.append("most approvals came within a minute of submission and no review has said no yet")
            if review["approved"] == 0:
                review_verdict = "unclear"
            elif draft_installs or self.auth_mode == "none" or rubber_stamp:
                review_verdict = "partly"
            else:
                review_verdict = "yes"
        # A bad copy is one whose bytes are not what was approved: hand-modified or gone. An outdated copy is
        # the channel moving on, which sync is meant to follow; it is counted, but it is not the question.
        sync_reasons = []
        if not check_marks:
            sync_verdict = "never checked"
            sync_reasons.append("no status or sync run reported in the window; install the session-start hook (BETA.md)")
        else:
            sync_reasons.append(
                f"checks reported from {checks['environments_checked']} environment(s) on {checks['days_with_checks']} day(s), last at {checks['last_check_at']}"
            )
            if sync["bad_copies"]:
                sync_verdict = "yes"
                sync_reasons.append(f"{sync['bad_copies']} bad cop{'y' if sync['bad_copies'] == 1 else 'ies'}: " + ", ".join(f"{k} {v}" for k, v in bad.items() if v))
                acted = ", ".join(f"{r} {by_response[r]}" for r in ("repaired", "forced", "kept_local") if by_response[r])
                sync_reasons.append("what happened next: " + (acted or "not synced yet"))
            else:
                sync_verdict = "no bad copy seen"
                sync_reasons.append("every check found the approved bytes (no modified or missing copy)")
        if by_state["outdated"]:
            sync_reasons.append(f"separately, {by_state['outdated']} outdated cop{'y' if by_state['outdated'] == 1 else 'ies'} found after a new release")

        return {
            "team": t["slug"],
            "generated_at": now(),
            "window_days": days,
            "since": since,
            "auth_mode": self.auth_mode,
            "schema_version": self.store.schema_version,
            "members": len(self.store.members(t["id"])),
            "skills": len(self.store.skills(t["id"])),
            "review": review,
            "sync": sync,
            "adoption": adoption,
            "answers": {
                "kept_review_on": {"verdict": review_verdict, "because": reasons},
                "sync_caught_bad_copy": {"verdict": sync_verdict, "because": sync_reasons},
            },
        }

    @rpc
    def dashboard(self, team: str, actor: str) -> dict:
        t, _ = self._ctx(team, actor)
        skills = self.list_skills(team, actor)
        by_status = {s: 0 for s in STATUSES}
        for s in skills:
            by_status[s["status"]] += 1
        most_installed = sorted((s for s in skills if s["installs"]), key=lambda s: (-s["installs"], s["slug"]))[:10]
        members = self.store.members(t["id"])
        return {
            "team": {"slug": t["slug"], "name": t["name"]},
            "skills_total": len(skills),
            "by_status": by_status,
            "members": len(members),
            "pending_reviews": self.pending_reviews(team, actor),
            "most_installed": [{"slug": s["slug"], "title": s["title"], "installs": s["installs"]} for s in most_installed],
            "drafts_in_progress": [
                {"slug": s["slug"], "editor": s["draft_editor"], "updated_at": s["draft_updated_at"]}
                for s in skills if s["has_draft"] and s["status"] != "in_review"
            ],
            "recent_activity": self.activity(team, actor, limit=15),
        }


RPC_OPS = sorted(name for name, fn in vars(Service).items() if getattr(fn, "rpc", False))
