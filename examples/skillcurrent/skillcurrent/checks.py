"""Deterministic, local checks a draft must pass before it can be submitted.

Every check is a transparent text rule with a one-line explanation of what it
looked for. No model is called and nothing is executed: a passing run is
specific evidence about these bytes, not a guarantee of how an agent will
behave. Teams extend the built-in set with their own rules (``require``,
``forbid`` and ``section`` patterns) that are stored in the catalog.
"""

import re

from . import frontmatter, skillfile

RULE_KINDS = ("require", "forbid", "section")
CATEGORIES = ("package", "selection", "safety", "authority", "evidence", "team")

_PLACEHOLDERS = (
    "describe the situations in which an agent should reach for this skill",
    "describe what this skill does and when an agent should use it",
    "first step.",
    "second step.",
    "what the skill must never do.",
    "lorem ipsum",
    "todo",
    "tbd",
    "…",
)
_SECRET_PATTERNS = (
    (r"AKIA[0-9A-Z]{16}", "an AWS access key id"),
    (r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}", "a GitHub token"),
    (r"\bsk-[A-Za-z0-9_-]{20,}", "an API secret key"),
    (r"\bxox[baprs]-[A-Za-z0-9-]{10,}", "a Slack token"),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "a private key"),
    (r"(?i)\b(password|passwd|secret|api[_-]?key|token)\s*[:=]\s*['\"]?[A-Za-z0-9/+_\-]{12,}", "a credential assignment"),
)
_USER_PATH = re.compile(r"(?:^|[\s(\"'`])(?:/Users/[^/\s]+|/home/[^/\s]+|[A-Za-z]:\\Users\\[^\\\s]+)")


def _headings(body: str) -> list[str]:
    return [line.lstrip("#").strip() for line in body.splitlines() if line.startswith("#")]


def _result(check_id: str, name: str, category: str, passed: bool, rule: str, detail: str = "") -> dict:
    return {"id": check_id, "name": name, "category": category, "passed": bool(passed), "rule": rule, "detail": detail}


def builtin(content: str) -> list[dict]:
    """Run the built-in checks against ``content`` and return one result per check."""
    results = []
    problems, warnings, doc = skillfile.check(content)
    results.append(
        _result(
            "front-matter", "Valid skill package", "package", not problems,
            "Local text check: the file parses as a SKILL.md with a valid name and description.",
            "; ".join(problems) if problems else "name and description parse cleanly",
        )
    )
    if doc is None:
        return results

    desc = doc.description.strip().lower()
    placeholder_hits = [p for p in _PLACEHOLDERS if p in desc]
    results.append(
        _result(
            "description", "Description says when to use the skill", "selection",
            20 <= len(doc.description) <= skillfile.DESCRIPTION_MAX and not placeholder_hits,
            "Local text check: the description is 20 to 1024 characters and is not template placeholder text.",
            f"{len(doc.description)} characters" + (f"; placeholder: {placeholder_hits[0]!r}" if placeholder_hits else ""),
        )
    )

    headings = _headings(doc.body)
    has_when = any(re.search(r"when to use|use this (skill|when)|trigger|applies when", h, re.I) for h in headings)
    has_method = any(re.search(r"steps|method|workflow|process|how to|procedure|instructions", h, re.I) for h in headings)
    results.append(
        _result(
            "sections", "Required skill sections", "package", has_when and has_method,
            "Local text check: the body has a heading that says when to use the skill and a heading with the steps or method.",
            f"headings: {', '.join(headings) if headings else 'none'}",
        )
    )

    body_lower = doc.body.lower()
    body_placeholders = [p for p in _PLACEHOLDERS if p in body_lower]
    results.append(
        _result(
            "placeholders", "No template placeholders left", "package", not body_placeholders,
            "Local text check: none of the template's placeholder phrases (and no TODO/TBD) remain in the body.",
            f"found: {body_placeholders[0]!r}" if body_placeholders else "no placeholder text found",
        )
    )

    secret_hit = None
    for pattern, label in _SECRET_PATTERNS:
        if re.search(pattern, content):
            secret_hit = label
            break
    results.append(
        _result(
            "secrets", "No embedded secrets", "safety", secret_hit is None,
            "Local text check of common credential shapes (AWS, GitHub, Slack, API keys, private keys, password assignments). Not a comprehensive secret scanner.",
            f"looks like {secret_hit}" if secret_hit else "no credential shapes found",
        )
    )

    path_hit = _USER_PATH.search(content)
    results.append(
        _result(
            "portable", "No machine-specific paths", "package", path_hit is None,
            "Local text check: no /Users/<name>, /home/<name> or C:\\Users\\<name> paths, so the skill works on every member's machine.",
            f"found {path_hit.group(0).strip()!r}" if path_hit else "no user-specific paths",
        )
    )

    ordered = [line for line in doc.body.splitlines() if re.match(r"^\s*\d+[.)]\s", line)]
    bulleted = [line for line in doc.body.splitlines() if re.match(r"^\s*[-*]\s", line)]
    results.append(
        _result(
            "actionable", "Method is written as steps", "evidence", bool(ordered) or len(bulleted) >= 2,
            "Local text check: the body contains a numbered list or at least two bullet points, so an agent has concrete actions to follow.",
            f"{len(ordered)} numbered lines, {len(bulleted)} bullets",
        )
    )
    return results


def team_rules(content: str, rules: list[dict]) -> list[dict]:
    """Evaluate team-defined rules. Each rule is a case-insensitive regex."""
    results = []
    try:
        meta_text, body = frontmatter.split(content)
    except frontmatter.FrontMatterError:
        meta_text, body = None, content
    headings = _headings(body)
    for rule in rules:
        try:
            regex = re.compile(rule["pattern"], re.I | re.M)
        except re.error as exc:
            results.append(_result(f"rule:{rule['name']}", rule["name"], rule.get("category", "team"), False, f"Team rule ({rule['kind']}): /{rule['pattern']}/", f"invalid pattern: {exc}"))
            continue
        kind = rule["kind"]
        if kind == "require":
            hit = regex.search(body)
            passed, detail = hit is not None, ("matched " + repr(hit.group(0)[:60]) if hit else "pattern not found in the body")
            rule_text = f"Team rule: the body must match /{rule['pattern']}/."
        elif kind == "forbid":
            hit = regex.search(body)
            passed, detail = hit is None, ("found " + repr(hit.group(0)[:60]) if hit else "pattern not present")
            rule_text = f"Team rule: the body must not match /{rule['pattern']}/."
        else:  # section
            hit = next((h for h in headings if regex.search(h)), None)
            passed, detail = hit is not None, (f"heading {hit!r}" if hit else "no heading matches")
            rule_text = f"Team rule: a heading must match /{rule['pattern']}/."
        if rule.get("rationale"):
            rule_text += f" {rule['rationale']}"
        results.append(_result(f"rule:{rule['name']}", rule["name"], rule.get("category", "team"), passed, rule_text, detail))
    return results


def run(content: str, rules: list[dict] | None = None) -> dict:
    results = builtin(content) + team_rules(content, rules or [])
    return {
        "passed": all(r["passed"] for r in results),
        "total": len(results),
        "failed": sum(1 for r in results if not r["passed"]),
        "results": results,
        "content_hash": skillfile.content_hash(content),
    }
