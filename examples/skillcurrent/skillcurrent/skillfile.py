"""Parsing, validation and rendering of ``SKILL.md`` files.

A skill file is Markdown with YAML front matter. ``name`` and ``description``
are required by every Agent-Skills host; SkillCurrent additionally understands
``version`` (stamped on publish), ``owner`` and ``tags``. Any other keys
(``license``, ``allowed-tools``, ``metadata``, ...) are preserved verbatim.
"""

import hashlib
import re
from dataclasses import dataclass, field

from . import frontmatter, semver
from .errors import Invalid

NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
NAME_MAX = 64
DESCRIPTION_MAX = 1024
KNOWN_KEYS = ("name", "description", "version", "owner", "tags")

TEMPLATE = """---
name: {name}
description: {description}
tags: []
---

# {title}

## When to use this skill

Describe the situations in which an agent should reach for this skill.

## Steps

1. First step.
2. Second step.

## Guardrails

- What the skill must never do.
"""


class SkillFileError(Invalid):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass
class SkillDoc:
    name: str
    description: str
    body: str
    version: str | None = None
    owner: str | None = None
    tags: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    @property
    def title(self) -> str:
        for line in self.body.splitlines():
            if line.startswith("# "):
                return line[2:].strip()
        return self.name

    def meta(self) -> dict:
        meta: dict = {"name": self.name, "description": self.description}
        if self.version:
            meta["version"] = self.version
        if self.owner:
            meta["owner"] = self.owner
        if self.tags:
            meta["tags"] = list(self.tags)
        for k, v in self.extra.items():
            meta[k] = v
        return meta

    def render(self) -> str:
        return frontmatter.render(self.meta(), self.body)


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-")
    return slug[:NAME_MAX].rstrip("-")


def _as_tags(value) -> tuple[list[str], list[str]]:
    problems = []
    if value in (None, ""):
        return [], problems
    if isinstance(value, str):
        value = [v.strip() for v in value.split(",") if v.strip()]
    if not isinstance(value, list):
        return [], ["tags must be a list of short words"]
    tags = []
    for tag in value:
        tag = str(tag).strip().lower()
        if not tag:
            continue
        if not NAME_RE.match(tag):
            problems.append(f"tag {tag!r} must be lowercase words joined by hyphens")
            continue
        if tag not in tags:
            tags.append(tag)
    return tags, problems


def check(text: str) -> tuple[list[str], list[str], SkillDoc | None]:
    """Return (problems, warnings, doc). ``doc`` is None when there are problems."""
    problems: list[str] = []
    warnings: list[str] = []
    try:
        meta_text, body = frontmatter.split(text)
    except frontmatter.FrontMatterError as exc:
        return [str(exc)], warnings, None
    if meta_text is None:
        return ["missing front matter: the file must start with a '---' block containing name and description"], warnings, None
    try:
        meta = frontmatter.parse(meta_text)
    except frontmatter.FrontMatterError as exc:
        return [f"front matter: {exc}"], warnings, None

    name = meta.get("name")
    if not isinstance(name, str) or not name.strip():
        problems.append("front matter 'name' is required")
        name = ""
    else:
        name = name.strip()
        if len(name) > NAME_MAX:
            problems.append(f"'name' must be at most {NAME_MAX} characters")
        if not NAME_RE.match(name):
            problems.append("'name' must be lowercase letters, digits and single hyphens (e.g. release-notes)")

    description = meta.get("description")
    if not isinstance(description, str) or not description.strip():
        problems.append("front matter 'description' is required")
        description = ""
    else:
        description = " ".join(description.split())
        if len(description) > DESCRIPTION_MAX:
            problems.append(f"'description' must be at most {DESCRIPTION_MAX} characters")
        elif len(description) < 20:
            warnings.append("'description' is very short; say what the skill does and when to use it")

    version = meta.get("version")
    if version not in (None, ""):
        version = str(version)
        if not semver.is_valid(version):
            problems.append("'version' must look like MAJOR.MINOR.PATCH")
    else:
        version = None

    owner = meta.get("owner")
    owner = str(owner).strip() if owner not in (None, "") else None

    tags, tag_problems = _as_tags(meta.get("tags"))
    problems.extend(tag_problems)

    body_stripped = body.strip()
    if not body_stripped:
        problems.append("the skill body (Markdown after the front matter) must not be empty")
    else:
        if not any(line.startswith("#") for line in body_stripped.splitlines()):
            warnings.append("body has no headings; structure it with sections such as 'When to use' and 'Steps'")
        if len(body_stripped) < 80:
            warnings.append("body is very short; agents follow skills better when steps are explicit")

    extra = {k: v for k, v in meta.items() if k not in KNOWN_KEYS}
    if problems:
        return problems, warnings, None
    return problems, warnings, SkillDoc(name, description, body, version, owner, tags, extra)


def parse(text: str) -> SkillDoc:
    problems, _warnings, doc = check(text)
    if problems or doc is None:
        raise SkillFileError(problems)
    return doc


def stamp_version(text: str, version: str) -> str:
    """Return ``text`` re-rendered with ``version`` set in the front matter."""
    doc = parse(text)
    doc.version = version
    return doc.render()


def template(name: str, description: str = "Describe what this skill does and when an agent should use it.") -> str:
    title = " ".join(part.capitalize() for part in name.split("-"))
    return TEMPLATE.format(name=name, description=description, title=title)
