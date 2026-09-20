"""Minimal YAML-subset front matter reader and writer.

Skill files use a small, predictable subset of YAML between two ``---``
fences: top-level ``key: value`` pairs whose value is a scalar, an inline
list (``[a, b]``), a block list of scalars or of flat mappings, a flat block
mapping (one level, as used by ``metadata:``), or a ``|``/``>`` block
scalar. That covers every Agent-Skills front matter in the wild and keeps
this package free of third-party dependencies.
"""

import json
import re

FENCE = "---"
_KEY_RE = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9_.\-]*)\s*:(.*)$")
_SPECIAL_LEAD = tuple('[{"\'#&*!|>%@`')


class FrontMatterError(ValueError):
    pass


def split(text: str) -> tuple[str | None, str]:
    """Split ``text`` into (front matter text or None, body)."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != FENCE:
        return None, text
    for i in range(1, len(lines)):
        if lines[i].strip() == FENCE:
            return "".join(lines[1:i]), "".join(lines[i + 1 :])
    raise FrontMatterError("front matter opened with '---' but never closed")


def _strip_comment(s: str) -> str:
    # A comment starts with '#' preceded by whitespace (or at column 0).
    out = []
    quote = None
    for i, ch in enumerate(s):
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
        elif ch == "#" and (i == 0 or s[i - 1] in " \t"):
            break
        out.append(ch)
    return "".join(out).rstrip()


def _scalar(raw: str):
    s = _strip_comment(raw.strip())
    if s == "":
        return ""
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            return s[1:-1]
    if len(s) >= 2 and s[0] == "'" and s[-1] == "'":
        return s[1:-1].replace("''", "'")
    low = s.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    if low in ("null", "~"):
        return None
    if re.fullmatch(r"-?(0|[1-9]\d*)", s):
        return int(s)
    return s


def _split_inline_list(inner: str) -> list[str]:
    items, buf, quote = [], [], None
    for ch in inner:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
            buf.append(ch)
        elif ch == ",":
            items.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf or items:
        items.append("".join(buf))
    return [i for i in (x.strip() for x in items) if i != ""]


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def _parse_mapping_lines(lines: list[str], lineno: int) -> dict:
    """Parse a flat ``key: scalar`` mapping from already-dedented lines."""
    out = {}
    for offset, line in enumerate(lines):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _KEY_RE.match(line.strip())
        if not m:
            raise FrontMatterError(f"line {lineno + offset}: expected 'key: value', got {line.strip()!r}")
        out[m.group(1)] = _scalar(m.group(2))
    return out


def _parse_block_list(block: list[str], lineno: int) -> list:
    items = []
    i = 0
    while i < len(block):
        line = block[i]
        if not line.strip():
            i += 1
            continue
        stripped = line.strip()
        if not (stripped == "-" or stripped.startswith("- ")):
            raise FrontMatterError(f"line {lineno + i}: expected a '- item' list entry, got {stripped!r}")
        base = _indent(line)
        payload = stripped[1:].strip()
        # Collect continuation lines indented deeper than the dash.
        j = i + 1
        cont = []
        while j < len(block) and (not block[j].strip() or _indent(block[j]) > base):
            cont.append(block[j])
            j += 1
        while cont and not cont[-1].strip():
            cont.pop()
        if payload and _KEY_RE.match(payload) and not payload.startswith(_SPECIAL_LEAD):
            mapping_lines = [payload] + [c.strip() for c in cont]
            items.append(_parse_mapping_lines(mapping_lines, lineno + i))
        elif cont:
            raise FrontMatterError(f"line {lineno + i}: nested structures under a scalar list item are not supported")
        elif payload.startswith("["):
            items.append(_parse_inline_list(payload, lineno + i))
        else:
            items.append(_scalar(payload))
        i = j
    return items


def _parse_inline_list(s: str, lineno: int) -> list:
    s = _strip_comment(s.strip())
    if not s.endswith("]"):
        raise FrontMatterError(f"line {lineno}: inline list must end with ']'")
    return [_scalar(x) for x in _split_inline_list(s[1:-1])]


def parse(text: str) -> dict:
    """Parse front matter text (without the fences) into a dict."""
    data: dict = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        lineno = i + 1
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        if line[0] in " \t":
            raise FrontMatterError(f"line {lineno}: unexpected indentation")
        m = _KEY_RE.match(line)
        if not m:
            raise FrontMatterError(f"line {lineno}: expected 'key: value', got {line!r}")
        key, rest = m.group(1), m.group(2)
        rest_s = _strip_comment(rest.strip())
        if rest_s in ("", "|", ">", "|-", ">-"):
            j = i + 1
            block = []
            while j < len(lines) and (not lines[j].strip() or lines[j][0] in " \t"):
                block.append(lines[j])
                j += 1
            while block and not block[-1].strip():
                block.pop()
            nonblank = [b for b in block if b.strip()]
            if rest_s in ("|", ">", "|-", ">-"):
                indent = min((_indent(b) for b in nonblank), default=0)
                content = [b[indent:] if b.strip() else "" for b in block]
                if rest_s.startswith("|"):
                    value = "\n".join(content)
                else:
                    value = " ".join(c.strip() for c in content if c.strip())
            elif not nonblank:
                value = ""
            elif nonblank[0].strip() == "-" or nonblank[0].strip().startswith("- "):
                value = _parse_block_list(block, lineno + 1)
            else:
                value = _parse_mapping_lines([b.strip() for b in block], lineno + 1)
            data[key] = value
            i = j
            continue
        if rest_s.startswith("["):
            data[key] = _parse_inline_list(rest_s, lineno)
        else:
            data[key] = _scalar(rest)
        i += 1
    return data


def _needs_quotes(s: str) -> bool:
    if s == "" or s != s.strip():
        return True
    if s.startswith(_SPECIAL_LEAD) or s.startswith("- ") or s == "-":
        return True
    if ": " in s or s.endswith(":") or " #" in s or "\n" in s:
        return True
    low = s.lower()
    if low in ("true", "false", "yes", "no", "null", "~"):
        return True
    if re.fullmatch(r"-?\d+(\.\d+)?", s):
        return True
    return False


def _dump_scalar(value) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    s = str(value)
    return json.dumps(s, ensure_ascii=False) if _needs_quotes(s) else s


def dump(meta: dict) -> str:
    """Render a dict as front matter text (without the fences)."""
    out = []
    for key, value in meta.items():
        if isinstance(value, str) and "\n" in value:
            out.append(f"{key}: |")
            out.extend("  " + line for line in value.split("\n"))
        elif isinstance(value, list):
            if not value:
                out.append(f"{key}: []")
                continue
            out.append(f"{key}:")
            for item in value:
                if isinstance(item, dict):
                    first = True
                    for k, v in item.items():
                        prefix = "  - " if first else "    "
                        out.append(f"{prefix}{k}: {_dump_scalar(v)}")
                        first = False
                    if first:
                        out.append("  - {}")
                else:
                    out.append(f"  - {_dump_scalar(item)}")
        elif isinstance(value, dict):
            if not value:
                out.append(f"{key}: {{}}")
                continue
            out.append(f"{key}:")
            for k, v in value.items():
                out.append(f"  {k}: {_dump_scalar(v)}")
        else:
            out.append(f"{key}: {_dump_scalar(value)}")
    return "\n".join(out) + ("\n" if out else "")


def render(meta: dict, body: str) -> str:
    body = body.lstrip("\n")
    if body and not body.endswith("\n"):
        body += "\n"
    return f"{FENCE}\n{dump(meta)}{FENCE}\n\n{body}" if body else f"{FENCE}\n{dump(meta)}{FENCE}\n"
