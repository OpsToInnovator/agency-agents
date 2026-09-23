"""Install released skills into the directories AI coding tools read them
from, and keep those copies in sync with the team catalog.

Each target is a directory that holds one ``<skill>/SKILL.md`` per skill,
which is the Agent-Skills layout shared by Claude Code, Antigravity, Osaurus,
Codex and others. Global targets live under the member's home directory;
project targets live under the current project.

An *environment* is a member on a host with one target. Every install and
every successful verification writes a receipt, which is what the team's
adoption view is built from.
"""

import os
import socket
from dataclasses import dataclass
from pathlib import Path

from . import skillfile
from .errors import Invalid, NotFound
from .session import require_identity

SKILL_FILE = "SKILL.md"
DEFAULT_CHANNEL = "production"


@dataclass(frozen=True)
class Target:
    id: str
    label: str
    base: str  # "~/..." for global targets, relative path for project targets, "" for custom
    scope: str  # "global" | "project" | "custom"

    def directory(self, home: Path | None = None, project: Path | None = None, custom_dir: str | Path | None = None) -> Path:
        if self.scope == "custom":
            if not custom_dir:
                raise Invalid("target 'custom' needs a directory (--dir)")
            return Path(custom_dir).expanduser().resolve()
        if self.scope == "global":
            root = Path(home) if home else Path(os.environ.get("SKILLCURRENT_HOME") or Path.home())
            return (root / self.base[2:]).resolve()
        root = Path(project) if project else Path.cwd()
        return (root / self.base).resolve()


TARGETS: dict[str, Target] = {
    t.id: t
    for t in (
        Target("claude-code", "Claude Code (global)", "~/.claude/skills", "global"),
        Target("claude-code-project", "Claude Code (this project)", ".claude/skills", "project"),
        Target("antigravity", "Antigravity (global)", "~/.gemini/config/skills", "global"),
        Target("antigravity-project", "Antigravity and others (this project's .agents/skills)", ".agents/skills", "project"),
        # Codex documents ~/.agents/skills for user skills (~/.codex/skills is its deprecated location).
        # Gemini CLI and Cursor document that they read it too.
        Target("codex", "Codex, Gemini CLI, Cursor (global ~/.agents/skills)", "~/.agents/skills", "global"),
        Target("osaurus", "Osaurus (global)", "~/.osaurus/skills", "global"),
        Target("custom", "Custom directory (--dir)", "", "custom"),
    )
}
DEFAULT_TARGET = "claude-code"
STATES = ("current", "outdated", "modified", "missing", "deprecated", "unreleased")


def target(target_id: str) -> Target:
    try:
        return TARGETS[target_id]
    except KeyError:
        raise Invalid(f"unknown target {target_id!r}; choose one of {', '.join(TARGETS)}") from None


def default_host() -> str:
    return os.environ.get("SKILLCURRENT_HOST") or socket.gethostname().split(".")[0]


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


class Installer:
    def __init__(self, session, home: Path | None = None, project: Path | None = None, custom_dir: str | Path | None = None, host: str | None = None):
        require_identity(session)
        self.session = session
        self.home = home
        self.project = project
        self.custom_dir = custom_dir
        self.host = host if host is not None else default_host()

    def _dir(self, target_id: str) -> Path:
        return target(target_id).directory(self.home, self.project, self.custom_dir)

    def path_for(self, slug: str, target_id: str) -> Path:
        return self._dir(target_id) / slug / SKILL_FILE

    def install(self, slug: str, target_id: str = DEFAULT_TARGET, ref: str | None = None, channel: str = DEFAULT_CHANNEL, reason: str = "",
                path: Path | None = None) -> dict:
        """Write the skill's release on ``channel`` (or an explicit ``ref``) to the target and record it.

        ``path`` overrides where the file goes; ``sync`` passes the recorded
        path so a repair lands where the copy was installed."""
        content = self.session.call("get_content", slug=slug, ref=ref or channel)
        path = Path(path) if path is not None else self.path_for(slug, target_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content["content"], encoding="utf-8")
        self.session.call(
            "record_install", slug=slug, version=content["version"], target=target_id,
            path=str(path), content_hash=content["content_hash"], host=self.host, channel=channel, reason=reason,
        )
        return {
            "slug": slug, "version": content["version"], "target": target_id, "path": str(path), "host": self.host,
            "channel": channel, "content_hash": content["content_hash"], "deprecated": content["lifecycle"] == "deprecated",
        }

    def uninstall(self, slug: str, target_id: str = DEFAULT_TARGET) -> dict:
        path = self.path_for(slug, target_id)
        removed_file = False
        if path.exists():
            path.unlink()
            removed_file = True
            try:
                path.parent.rmdir()  # only if empty
            except OSError:
                pass
        result = self.session.call("remove_install", slug=slug, target=target_id, host=self.host)
        if not removed_file and not result["removed"]:
            raise NotFound(f"{slug!r} is not installed for target {target_id!r} on this host")
        return {"slug": slug, "target": target_id, "removed_file": removed_file, "removed_record": result["removed"]}

    def status(self, target_id: str | None = None, report: bool = True) -> list[dict]:
        """Compare every recorded install on this host with disk and with the channel it follows.

        A copy whose bytes still match what was installed sends a ``verified``
        receipt, so the team's adoption view shows it was seen recently.
        """
        rows = self.session.call("list_installs", host=self.host)
        out = []
        for row in rows:
            if target_id and row["target"] != target_id:
                continue
            if row["target"] not in TARGETS:
                continue
            path = Path(row["path"])
            on_disk = _read(path)
            intact = on_disk is not None and skillfile.content_hash(on_disk) == row["content_hash"]
            if on_disk is None:
                state = "missing"
            elif not intact:
                state = "modified"
            elif row["lifecycle"] == "deprecated":
                state = "deprecated"
            elif row["target_version"] is None:
                state = "unreleased"
            elif row["target_version"] != row["version"]:
                state = "outdated"
            else:
                state = "current"
            if intact and report:
                self.session.call("report", slug=row["slug"], event="verified", target=row["target"], host=self.host, version=row["version"], content_hash=row["content_hash"])
            if report and state in ("modified", "missing", "outdated"):
                self._drift(row["slug"], row["target"], state, row["version"], row["target_version"], "observed")
            out.append(
                {
                    "slug": row["slug"], "target": row["target"], "path": row["path"], "installed": row["version"],
                    "channel": row["channel"], "target_version": row["target_version"], "state": state, "lifecycle": row["lifecycle"],
                }
            )
        return out

    def _drift(self, slug: str, target_id: str, state: str, installed: str, channel_version: str | None, response: str) -> None:
        self.session.call(
            "report_drift", slug=slug, target=target_id, state=state, host=self.host,
            installed_version=installed or "", channel_version=channel_version or "", response=response,
        )

    def sync(self, target_id: str | None = None, force: bool = False) -> list[dict]:
        """Bring installs back to what their channel serves. Locally modified copies are kept unless ``force``.

        Every repair (and every modified copy it leaves alone) is reported to
        the server, so the team can later tell whether sync ever caught a bad copy."""
        actions = []
        for item in self.status(target_id):
            state = item["state"]
            if state in ("current", "deprecated", "unreleased"):
                actions.append({**item, "action": "kept"})
            elif state == "modified" and not force:
                self._drift(item["slug"], item["target"], state, item["installed"], item["target_version"], "kept_local")
                actions.append({**item, "action": "skipped", "reason": "local changes; use --force to overwrite"})
            else:
                # Repair in place: a custom or project copy lives where it was installed, not where this command runs.
                # The path comes from the server, so only ever write a file shaped like <slug>/SKILL.md.
                recorded = Path(item["path"])
                if recorded.name != SKILL_FILE or recorded.parent.name != item["slug"]:
                    actions.append({**item, "action": "skipped", "reason": f"recorded path is not {item['slug']}/{SKILL_FILE}; run install again"})
                    continue
                result = self.install(item["slug"], item["target"], channel=item["channel"], reason=f"sync:{state}", path=recorded)
                self._drift(item["slug"], item["target"], state, item["installed"], item["target_version"], "forced" if state == "modified" else "repaired")
                actions.append({**item, "action": "updated", "installed": result["version"]})
        return actions

    def report(self, slug: str, event: str, target_id: str = DEFAULT_TARGET, detail: str = "") -> dict:
        """Send a ``loaded`` or ``task_tested`` receipt for an install on this host (for tool hooks and test runners)."""
        return self.session.call("report", slug=slug, event=event, target=target_id, host=self.host, detail=detail)
