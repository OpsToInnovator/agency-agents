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

from . import semver, skillfile
from .errors import Conflict, Forbidden, Invalid, NotFound
from .store import Store, now

ROLES = ("viewer", "contributor", "maintainer", "owner")
RANK = {role: i for i, role in enumerate(ROLES)}
STATUSES = ("draft", "in_review", "published", "deprecated")
HANDLE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
SLUG_RE = skillfile.NAME_RE
TOKEN_PREFIX = "ts_"


def rpc(fn):
    fn.rpc = True
    return fn


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(24)


class Service:
    def __init__(self, store: Store):
        self.store = store

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

    def _status(self, skill: dict, pending: dict | None) -> str:
        if skill["lifecycle"] == "deprecated":
            return "deprecated"
        if pending is not None:
            return "in_review"
        if skill["latest_version_id"]:
            return "published"
        return "draft"

    def _skill_view(self, skill: dict, install_count: int | None = None) -> dict:
        pending = self.store.pending_review(skill["id"])
        latest = self.store.version_by_id(skill["latest_version_id"]) if skill["latest_version_id"] else None
        view = {
            "slug": skill["slug"],
            "title": skill["title"],
            "description": skill["description"],
            "owner": skill["owner_handle"],
            "tags": json.loads(skill["tags"]),
            "status": self._status(skill, pending),
            "lifecycle": skill["lifecycle"],
            "deprecation_reason": skill["deprecation_reason"],
            "latest_version": latest["version"] if latest else None,
            "published_at": latest["created_at"] if latest else None,
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

        ``ref`` is None/"latest" (latest published), "draft", or a version.
        """
        if ref in (None, "", "latest"):
            if not skill["latest_version_id"]:
                raise NotFound(f"{skill['slug']!r} has no published version yet (use ref='draft')")
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
            from_label, _, a = self._resolve_ref(skill, from_ref)
        except NotFound:
            if from_ref in (None, "", "latest"):
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
    def approve(self, team: str, actor: str, slug: str, comment: str = "") -> dict:
        t, m = self._ctx(team, actor)
        self._require(m, "maintainer", "approving skills")
        skill = self._skill(t, slug)
        pending = self.store.pending_review(skill["id"])
        if pending is None:
            raise Invalid(f"{slug!r} has no pending review")
        if pending["submitted_by"] == actor:
            raise Forbidden("a submission must be approved by a different maintainer")
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
            self._log(t, actor, "skill.published", slug, version=version, author=pending["submitted_by"], comment=comment or "")
        out = self._skill_view(self._skill(t, slug))
        out["published"] = self._version_view(self.store.version_by_id(version_id))
        return out

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
    def record_install(self, team: str, actor: str, slug: str, version: str, target: str, path: str, content_hash: str) -> dict:
        t, _ = self._ctx(team, actor)
        skill = self._skill(t, slug)
        if version != "draft" and self.store.version(skill["id"], version) is None:
            raise NotFound(f"{slug!r} has no version {version!r}")
        with self.store.tx():
            self.store.upsert_install(t["id"], actor, skill["id"], version, target, path, content_hash)
            self._log(t, actor, "skill.installed", slug, version=version, target=target)
        return {"slug": slug, "version": version, "target": target, "path": path}

    @rpc
    def remove_install(self, team: str, actor: str, slug: str, target: str) -> dict:
        t, _ = self._ctx(team, actor)
        skill = self._skill(t, slug)
        with self.store.tx():
            removed = self.store.delete_install(t["id"], actor, skill["id"], target)
            if removed:
                self._log(t, actor, "skill.uninstalled", slug, target=target)
        return {"slug": slug, "target": target, "removed": bool(removed)}

    @rpc
    def list_installs(self, team: str, actor: str, handle: str | None = None) -> list[dict]:
        t, m = self._ctx(team, actor)
        if handle == "*":
            self._require(m, "maintainer", "listing every member's installs")
            rows = self.store.installs(t["id"])
        else:
            handle = handle or actor
            if handle != actor:
                self._require(m, "maintainer", "listing another member's installs")
            rows = self.store.installs(t["id"], handle)
        skills = {s["id"]: s for s in self.store.skills(t["id"])}
        out = []
        for r in rows:
            skill = skills[r["skill_id"]]
            latest = self.store.version_by_id(skill["latest_version_id"]) if skill["latest_version_id"] else None
            out.append(
                {
                    "handle": r["handle"],
                    "slug": r["skill_slug"],
                    "version": r["version"],
                    "target": r["target"],
                    "path": r["path"],
                    "content_hash": r["content_hash"],
                    "installed_at": r["installed_at"],
                    "latest_version": latest["version"] if latest else None,
                    "latest_hash": latest["content_hash"] if latest else None,
                    "lifecycle": skill["lifecycle"],
                }
            )
        return out

    @rpc
    def catalog_index(self, team: str, actor: str) -> dict:
        """A signed-off view of what is published: one entry per skill with its
        version and content digest, plus a digest of the whole index so a
        runtime can prove it retrieved the same generation as another."""
        t, _ = self._ctx(team, actor)
        entries = []
        for skill in self.store.skills(t["id"]):
            if not skill["latest_version_id"]:
                continue
            v = self.store.version_by_id(skill["latest_version_id"])
            entries.append(
                {
                    "slug": skill["slug"], "version": v["version"], "content_hash": v["content_hash"],
                    "published_at": v["created_at"], "lifecycle": skill["lifecycle"],
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
    def activity(self, team: str, actor: str, limit: int = 50, slug: str | None = None) -> list[dict]:
        t, _ = self._ctx(team, actor)
        limit = max(1, min(int(limit), 500))
        return [
            {"actor": a["actor"], "action": a["action"], "skill": a["skill_slug"], "details": a["details"], "at": a["created_at"]}
            for a in self.store.activity(t["id"], limit, slug)
        ]

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
