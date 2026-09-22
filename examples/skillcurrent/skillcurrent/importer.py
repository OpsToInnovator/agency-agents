"""Bring existing skill files, or Agency agent files, into the catalog.

Accepts a single ``.md`` file, a directory of ``*.md`` files (like this
repository's division directories), or a directory of ``<name>/SKILL.md``
folders. Agent files whose ``name`` is a display name ("Backend Architect")
are slugified ("backend-architect") so they become valid skills.
"""

from pathlib import Path

from . import frontmatter, skillfile
from .errors import Conflict, SkillCurrentError


def collect_sources(path: str | Path) -> list[tuple[Path, str]]:
    root = Path(path)
    if root.is_file():
        return [(root, root.read_text(encoding="utf-8"))]
    if not root.is_dir():
        raise FileNotFoundError(f"{root} is neither a file nor a directory")
    files = sorted(root.glob("*/SKILL.md")) + sorted(p for p in root.glob("*.md") if p.name.upper() != "README.MD")
    return [(p, p.read_text(encoding="utf-8")) for p in files]


def normalize(text: str) -> tuple[str | None, list[str]]:
    """Return (skill content, problems). Agent-style front matter is coerced into a skill."""
    try:
        meta_text, body = frontmatter.split(text)
        meta = frontmatter.parse(meta_text or "")
    except frontmatter.FrontMatterError as exc:
        return None, [str(exc)]
    if meta_text is None:
        return None, ["missing front matter"]
    name = meta.get("name")
    if isinstance(name, str) and name.strip() and not skillfile.NAME_RE.match(name.strip()):
        meta["name"] = skillfile.slugify(name)
    meta = {k: v for k, v in meta.items() if k not in ("color", "emoji", "vibe", "services")}
    candidate = frontmatter.render(meta, body)
    problems, _warnings, _doc = skillfile.check(candidate)
    return (candidate if not problems else None), problems


def import_path(session, path: str | Path, update: bool = False) -> list[dict]:
    results = []
    for source, text in collect_sources(path):
        content, problems = normalize(text)
        if content is None:
            results.append({"source": str(source), "action": "skipped", "reason": "; ".join(problems)})
            continue
        slug = skillfile.parse(content).name
        try:
            session.call("create_skill", content=content)
            results.append({"source": str(source), "slug": slug, "action": "created"})
        except Conflict:
            if not update:
                results.append({"source": str(source), "slug": slug, "action": "skipped", "reason": "already exists (use --update)"})
                continue
            try:
                session.call("update_draft", slug=slug, content=content)
                results.append({"source": str(source), "slug": slug, "action": "updated"})
            except SkillCurrentError as exc:
                results.append({"source": str(source), "slug": slug, "action": "skipped", "reason": exc.message})
        except SkillCurrentError as exc:
            results.append({"source": str(source), "slug": slug, "action": "skipped", "reason": exc.message})
    return results
