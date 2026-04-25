"""Tiny YAML front-matter emitter and splitter.

We don't pull in PyYAML for this — our values are simple (strings, ints, bools,
None, flat lists of strings). Strings always get double-quoted with `"` and
`\\` escaped. Newlines in strings are collapsed to spaces so each value stays
on one line.
"""

from __future__ import annotations

import re
from typing import Any

_SEP = "---"


def _yaml_str(s: str) -> str:
    s = s.replace("\\", "\\\\").replace('"', '\\"')
    s = s.replace("\n", " ").replace("\r", " ")
    return f'"{s}"'


def _yaml_value(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_yaml_value(x) for x in v) + "]"
    return _yaml_str(str(v))


def to_front_matter(data: dict[str, Any]) -> str:
    """Render a dict as a YAML front-matter block, including the `---` lines.

    Output ends with a trailing newline after the closing separator so callers
    can simply concatenate it with the body.
    """
    lines = [_SEP]
    for k, v in data.items():
        lines.append(f"{k}: {_yaml_value(v)}")
    lines.append(_SEP)
    return "\n".join(lines) + "\n"


def split_front_matter(text: str) -> tuple[dict[str, Any], str]:
    """Return (front_matter_dict, body_text). When no front matter is present,
    returns ({}, text) unchanged.
    """
    if not text.startswith(_SEP + "\n") and not text.startswith(_SEP + "\r\n"):
        return {}, text
    end_idx = text.find("\n" + _SEP, len(_SEP))
    if end_idx < 0:
        return {}, text
    block = text[len(_SEP) : end_idx].strip()
    after = text[end_idx + 1 + len(_SEP) :].lstrip("\n")
    return _parse_block(block), after


def strip_front_matter(text: str) -> str:
    return split_front_matter(text)[1]


def _parse_block(block: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for line in block.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$", line)
        if not m:
            continue
        key, raw = m.group(1), m.group(2).strip()
        out[key] = _parse_value(raw)
    return out


def _parse_value(raw: str) -> Any:
    if raw == "null" or raw == "":
        return None
    if raw == "true":
        return True
    if raw == "false":
        return False
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        if not inner:
            return []
        return [_parse_value(p.strip()) for p in _split_list(inner)]
    if raw.startswith('"') and raw.endswith('"'):
        s = raw[1:-1]
        s = s.replace('\\"', '"').replace("\\\\", "\\")
        return s
    # numbers
    try:
        if "." in raw:
            return float(raw)
        return int(raw)
    except ValueError:
        return raw  # bare token, return as string


def _split_list(s: str) -> list[str]:
    """Split a YAML inline list body, respecting double-quoted strings."""
    parts: list[str] = []
    buf: list[str] = []
    in_str = False
    escape = False
    for ch in s:
        if escape:
            buf.append(ch)
            escape = False
            continue
        if ch == "\\":
            buf.append(ch)
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            buf.append(ch)
            continue
        if ch == "," and not in_str:
            parts.append("".join(buf).strip())
            buf = []
            continue
        buf.append(ch)
    if buf:
        parts.append("".join(buf).strip())
    return parts


def cap_description(text: str, max_chars: int = 280) -> str:
    """Cap a description string at max_chars. Truncate at the last word
    boundary and trim trailing punctuation/whitespace, append `…`.
    """
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    # Reserve 1 char for the ellipsis
    cut = text[: max_chars - 1]
    # Walk back to last whitespace
    last_space = cut.rfind(" ")
    if last_space > max_chars * 0.5:
        cut = cut[:last_space]
    cut = cut.rstrip().rstrip(",.;:!-—–")
    return cut + "…"
