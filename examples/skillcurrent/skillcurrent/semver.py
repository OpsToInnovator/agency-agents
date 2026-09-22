"""Tiny semantic-version helpers (MAJOR.MINOR.PATCH only)."""

import re

_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
BUMPS = ("major", "minor", "patch")
INITIAL = "1.0.0"


def is_valid(version: str) -> bool:
    return bool(_RE.match(version or ""))


def parse(version: str) -> tuple[int, int, int]:
    m = _RE.match(version or "")
    if not m:
        raise ValueError(f"not a MAJOR.MINOR.PATCH version: {version!r}")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def bump(version: str, kind: str) -> str:
    major, minor, patch = parse(version)
    if kind == "major":
        return f"{major + 1}.0.0"
    if kind == "minor":
        return f"{major}.{minor + 1}.0"
    if kind == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise ValueError(f"unknown bump kind {kind!r}; expected one of {', '.join(BUMPS)}")


def compare(a: str, b: str) -> int:
    pa, pb = parse(a), parse(b)
    return (pa > pb) - (pa < pb)
