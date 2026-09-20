"""Install published skills into the directories AI coding tools read them
from, and keep those copies in sync with the team catalog.

Each target is a directory that holds one ``<skill>/SKILL.md`` per skill,
which is the Agent-Skills layout shared by Claude Code, Antigravity, Osaurus,
Codex and others. Global targets live under the member's home directory;
project targets live under the current project.
"""

import os
from dataclasses import dataclass
from pathlib import Path

from . import skillfile
from .errors import Invalid, NotFound
from .session import require_identity

SKILL_FILE = "SKILL.md"


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
            root = Path(home) if home else Path(os.environ.get("TEAMSKILLS_HOME") or Path.home())
            return (root / self.base[2:]).resolve()
        root = Path(project) if project else Path.cwd()
        return (root / self.base).resolve()


TARGETS: dict[str, Target] = {
    t.id: t
    for t in (
        Target("claude-code", "Claude Code (global)", "~/.claude/skills", "global"),
        Target("claude-code-project", "Claude Code (this project)", ".claude/skills", "project"),
        Target("antigravity", "Antigravity / Gemini (global)", "~/.gemini/config/skills", "global"),
        Target("antigravity-project", "Antigravity / Gemini (this project)", ".agents/skills", "project"),
        Target("codex", "Codex (global)", "~/.codex/skills", "global"),
        Target("osaurus", "Osaurus (global)", "~/.osaurus/skills", "global"),
        Target("custom", "Custom directory (--dir)", "", "custom"),
    )
}
DEFAULT_TARGET = "claude-code"
STATES = ("current", "outdated", "modified", "missing", "deprecated")


def target(target_id: str) -> Target:
    try:
        return TARGETS[target_id]
    except KeyError:
        raise Invalid(f"unknown target {target_id!r}; choose one of {', '.join(TARGETS)}") from None


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


class Installer:
    def __init__(self, session, home: Path | None = None, project: Path | None = None, custom_dir: str | Path | None = None):
        require_identity(session)
        self.session = session
        self.home = home
        self.project = project
        self.custom_dir = custom_dir

    def _dir(self, target_id: str) -> Path:
        return target(target_id).directory(self.home, self.project, self.custom_dir)

    def path_for(self, slug: str, target_id: str) -> Path:
        return self._dir(target_id) / slug / SKILL_FILE

    def install(self, slug: str, target_id: str = DEFAULT_TARGET, ref: str | None = None) -> dict:
        """Write the published skill (or ``ref``) to the target and record it."""
        content = self.session.call("get_content", slug=slug, ref=ref)
        path = self.path_for(slug, target_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content["content"], encoding="utf-8")
        self.session.call(
            "record_install", slug=slug, version=content["version"], target=target_id,
            path=str(path), content_hash=content["content_hash"],
        )
        return {
            "slug": slug, "version": content["version"], "target": target_id, "path": str(path),
            "deprecated": content["lifecycle"] == "deprecated",
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
        result = self.session.call("remove_install", slug=slug, target=target_id)
        if not removed_file and not result["removed"]:
            raise NotFound(f"{slug!r} is not installed for target {target_id!r}")
        return {"slug": slug, "target": target_id, "removed_file": removed_file, "removed_record": result["removed"]}

    def status(self, target_id: str | None = None) -> list[dict]:
        """Compare every recorded install for the current member with disk and the catalog."""
        rows = self.session.call("list_installs")
        out = []
        for row in rows:
            if target_id and row["target"] != target_id:
                continue
            if row["target"] not in TARGETS:
                continue
            path = Path(row["path"])
            on_disk = _read(path)
            if on_disk is None:
                state = "missing"
            elif skillfile.content_hash(on_disk) != row["content_hash"]:
                state = "modified"
            elif row["lifecycle"] == "deprecated":
                state = "deprecated"
            elif row["latest_version"] and row["latest_version"] != row["version"]:
                state = "outdated"
            else:
                state = "current"
            out.append(
                {
                    "slug": row["slug"], "target": row["target"], "path": row["path"], "installed": row["version"],
                    "latest": row["latest_version"], "state": state, "lifecycle": row["lifecycle"],
                }
            )
        return out

    def sync(self, target_id: str | None = None, force: bool = False) -> list[dict]:
        """Reinstall outdated or missing skills. Locally modified copies are kept unless ``force``."""
        actions = []
        for item in self.status(target_id):
            state = item["state"]
            if state == "current" or state == "deprecated":
                actions.append({**item, "action": "kept"})
            elif state == "modified" and not force:
                actions.append({**item, "action": "skipped", "reason": "local changes; use --force to overwrite"})
            else:
                result = self.install(item["slug"], item["target"])
                actions.append({**item, "action": "updated", "installed": result["version"]})
        return actions
