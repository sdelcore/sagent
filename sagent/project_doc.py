"""The structure of `project.md` — parse, render, derive.

Everything that touches the shape of a per-project digest lives here:

  - parsing the LLM's `DESCRIPTION:` / `TAGLINE:` preamble
  - dropping any prior auto-injected headline block while preserving the
    user's hand-written preamble (a load-bearing safety: users may add
    their own notes between the H1 and the first `##`, and a refresh
    must not destroy them)
  - building the auto-derived headline block (quote + stats line) from
    the front-matter values
  - counting `- ` bullets under each `## Section` for front-matter counts
  - building the front-matter dict from doc fields plus session stats
  - bucketing momentum and days-since-last-session
  - diffing two front-matter dicts into a single changelog line

The rollup module owns the LLM call, file I/O, and the changelog file
itself; this module owns the document type.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .frontmatter import cap_description


# ---------------------------------------------------------------------------
# ProjectDoc
# ---------------------------------------------------------------------------


@dataclass
class ProjectDoc:
    """Parsed shape of the body of `project.md` (without front matter).

    Attributes:
      - `name`: the project name as it appears in the H1 and front matter
      - `description`: stable one-or-two-sentence description (capped at 280)
      - `tagline`: volatile "what's in flight now" line
      - `body`: the markdown after the H1, with any prior auto-injected
        headline block stripped. Hand-written user preamble between the
        H1 and the first `##` is preserved.
      - `has_h1`: whether `parse()` actually found an H1. When False,
        `render_body()` emits `body` raw (matches the legacy bail-out
        when the LLM produced output without an H1 line).

    Render is the inverse of parse: the H1 is reconstructed from `name`,
    a fresh headline block is injected from front-matter values, and the
    preserved `body` is appended.
    """

    name: str
    description: str = ""
    tagline: str = ""
    body: str = ""  # markdown after H1, headline-stripped
    has_h1: bool = True

    # ------------------------------------------------------------------
    # Parse
    # ------------------------------------------------------------------

    @classmethod
    def parse(cls, llm_output: str, *, name: str) -> "ProjectDoc":
        """Parse a fresh LLM-emitted document into a ProjectDoc.

        The expected shape is:

            DESCRIPTION: <text>
            TAGLINE: <text>

            # <name>
            <optional preamble>
            ## Section 1
            ...

        Tolerant: if DESCRIPTION/TAGLINE are missing they default to "".
        If the body lacks an H1, the entire string after DESC/TAG becomes
        `body` and render-time headline injection is skipped.
        """
        text = _strip_code_fence(llm_output).strip()
        description, tagline, after_dt = _extract_description_tagline(text)
        body_after_h1, has_h1 = _drop_h1_and_strip_prior_headline(after_dt)
        return cls(
            name=name,
            description=description,
            tagline=tagline,
            body=body_after_h1,
            has_h1=has_h1,
        )

    # ------------------------------------------------------------------
    # Render
    # ------------------------------------------------------------------

    def render_body(self, *, front_matter: dict[str, Any]) -> str:
        """Return the markdown body (no front-matter block).

        Emits H1 + freshly-derived headline + preserved `body`. If parse
        could not find an H1 in the LLM output, returns `body` unchanged
        (matches the legacy `_inject_headline_block` no-H1 bail-out).
        """
        if not self.has_h1 or not self.name:
            return self.body

        lines: list[str] = [f"# {self.name}", ""]
        headline = build_headline_block(self.name, front_matter)
        if headline:
            lines.extend(headline)
            lines.append("")

        body = self.body.lstrip("\n")
        if body:
            lines.append(body.rstrip("\n"))

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Derive
    # ------------------------------------------------------------------

    def section_bullet_counts(self) -> dict[str, int]:
        """Count `- ` bullets under each `## Section` heading in `body`."""
        return _count_section_bullets(self.body)

    def derive_front_matter(
        self,
        *,
        sessions_dir: Path,
        now: float | None = None,
        last_updated: str | None = None,
    ) -> dict[str, Any]:
        """Build the front-matter dict from doc fields and session stats.

        `sessions_dir` is the directory of per-session `.md` files; mtimes
        drive the recent-activity counts. `now` and `last_updated` are
        injectable for deterministic tests.
        """
        if now is None:
            now = time.time()
        if last_updated is None:
            last_updated = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)
            )

        session_files = (
            sorted(sessions_dir.glob("*.md")) if sessions_dir.exists() else []
        )
        cutoff_7d = now - 7 * 86_400
        cutoff_14d = now - 14 * 86_400
        sessions_last_7d = sum(
            1 for f in session_files if f.stat().st_mtime >= cutoff_7d
        )
        sessions_prior_7d = sum(
            1
            for f in session_files
            if cutoff_14d <= f.stat().st_mtime < cutoff_7d
        )

        counts = self.section_bullet_counts()

        return {
            "type": "project",
            "source": "claude-code",
            "project": self.name,
            "description": self.description,
            "tagline": self.tagline,
            "last_updated": last_updated,
            "session_count": len(session_files),
            "sessions_last_7d": sessions_last_7d,
            "days_since_last_session": _days_since_last_session(
                session_files, now=now
            ),
            "momentum": momentum_bucket(sessions_last_7d, sessions_prior_7d),
            "decisions": counts.get("Long-term decisions", 0)
            + counts.get("Decisions", 0),
            "open_threads": counts.get("Open threads", 0),
            "preferences": counts.get("User preferences", 0)
            + counts.get("Preferences", 0),
            "risks": counts.get("Risks & known issues", 0)
            + counts.get("Risks", 0)
            + counts.get("Risks & blockers", 0),
        }


# ---------------------------------------------------------------------------
# Headline block (derived view; never stored)
# ---------------------------------------------------------------------------


def build_headline_block(project_name: str, fm: dict[str, Any]) -> list[str]:
    """Quote+stats lines that mirror front matter into the body.

    Returns lines without trailing blank. Fields not present in `fm` are
    omitted gracefully.
    """
    description = (fm.get("description") or "").strip()
    tagline = (fm.get("tagline") or "").strip()

    quote_lines: list[str] = []
    if description:
        quote_lines.append(f"> **{project_name}** — {description}")
    else:
        quote_lines.append(f"> **{project_name}**")
    if tagline:
        quote_lines.append(f"> **Now:** {tagline}")

    stats_bits: list[str] = []
    for fld, label in (
        ("decisions", "decisions"),
        ("open_threads", "open"),
        ("risks", "risks"),
    ):
        v = fm.get(fld)
        if v:
            stats_bits.append(f"{v} {label}")
    days = fm.get("days_since_last_session")
    if isinstance(days, int) and days >= 0:
        stats_bits.append(f"last session {days}d ago")
    momentum = fm.get("momentum")
    if momentum:
        stats_bits.append(f"momentum: {momentum}")

    lines = list(quote_lines)
    if stats_bits:
        lines.append("")
        lines.append("`" + " · ".join(stats_bits) + "`")
    return lines


# ---------------------------------------------------------------------------
# Changelog diff — pure function on two front-matter dicts
# ---------------------------------------------------------------------------


# Field order matters: this is the order they appear in the delta line.
# Tuples: (frontmatter_key, singular_label, plural_label).
_CHANGELOG_COUNT_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("decisions", "decision", "decisions"),
    ("open_threads", "open", "open"),
    ("preferences", "preference", "preferences"),
    ("risks", "risk", "risks"),
)


def diff_front_matter(prior_fm: dict, new_fm: dict) -> str:
    """Build one changelog line from `prior_fm` → `new_fm`.

    Returns "" if every numeric field is unchanged (the line is suppressed).
    Only fields whose count actually changed are listed. Momentum is
    included only when the bucket transitions to a different value.
    """
    parts: list[str] = []
    for key, singular, plural in _CHANGELOG_COUNT_FIELDS:
        prior = _as_int(prior_fm.get(key))
        new = _as_int(new_fm.get(key))
        diff = new - prior
        if diff == 0:
            continue
        sign = "+" if diff > 0 else "-"
        label = singular if abs(diff) == 1 else plural
        parts.append(f"{sign}{abs(diff)} {label}")

    prior_sessions = _as_int(prior_fm.get("sessions_last_7d"))
    new_sessions = _as_int(new_fm.get("sessions_last_7d"))
    sessions_changed = prior_sessions != new_sessions

    prior_momentum = prior_fm.get("momentum")
    new_momentum = new_fm.get("momentum")
    momentum_changed = (
        new_momentum is not None
        and prior_momentum is not None
        and prior_momentum != new_momentum
    )

    if not parts and not sessions_changed and not momentum_changed:
        return ""

    segments: list[str] = []
    if parts:
        segments.append(", ".join(parts))
    if sessions_changed:
        segments.append(f"sessions_last_7d {prior_sessions}→{new_sessions}")
    if momentum_changed:
        segments.append(f"momentum {prior_momentum}→{new_momentum}")

    timestamp = str(new_fm.get("last_updated") or "").strip()
    if not timestamp:
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return f"- {timestamp} — " + " · ".join(segments)


# ---------------------------------------------------------------------------
# Stats helpers used by derive_front_matter()
# ---------------------------------------------------------------------------


def _days_since_last_session(
    session_files: list[Path], *, now: float
) -> int | None:
    """Whole days between `now` and the newest session file's mtime.

    Returns None when there are no sessions so the caller can omit/null it.
    """
    if not session_files:
        return None
    newest = max(f.stat().st_mtime for f in session_files)
    delta = max(0.0, now - newest)
    return int(delta // 86_400)


def momentum_bucket(last_7d: int, prior_7d: int) -> str:
    """Bucket recent activity vs the prior 7d window.

    cold     — 0 sessions in last 7d
    cooling  — last 7d < prior 7d (and last 7d > 0)
    steady   — last 7d == prior 7d
    rising   — last 7d > prior 7d
    """
    if last_7d == 0:
        return "cold"
    if last_7d > prior_7d:
        return "rising"
    if last_7d < prior_7d:
        return "cooling"
    return "steady"


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Parse helpers
# ---------------------------------------------------------------------------


def _strip_code_fence(text: str) -> str:
    """Strip a wrapping ```...``` fence if the LLM took the prompt format
    literally and added one."""
    t = text.strip()
    if not t.startswith("```"):
        return text
    nl = t.find("\n")
    if nl < 0:
        return text
    body = t[nl + 1 :]
    if body.rstrip().endswith("```"):
        body = body.rstrip()[:-3]
    return body


def _extract_description_tagline(body: str) -> tuple[str, str, str]:
    """Pull DESCRIPTION: and TAGLINE: lines off the top of the document.

    Returns (description, tagline, remaining_body). Tolerant: if either
    line is missing, returns "" for it and leaves the remaining body
    untouched for that line.
    """
    description = ""
    tagline = ""
    lines = body.splitlines()
    consumed = 0
    for line in lines:
        if not line.strip():
            consumed += 1
            continue
        m_desc = re.match(r"^DESCRIPTION:\s*(.*)$", line)
        m_tag = re.match(r"^TAGLINE:\s*(.*)$", line)
        if m_desc and not description:
            description = m_desc.group(1).strip()
            consumed += 1
            continue
        if m_tag and not tagline:
            tagline = m_tag.group(1).strip()
            consumed += 1
            continue
        break
    description = cap_description(description, max_chars=280)
    remaining = "\n".join(lines[consumed:]).lstrip()
    return description, tagline, remaining


def _drop_h1_and_strip_prior_headline(body: str) -> tuple[str, bool]:
    """Return (body_after_h1, has_h1).

    `body_after_h1` is the markdown after the H1 with any prior
    auto-injected headline block stripped. Hand-written user preamble
    between H1 and first `##` is preserved. If there is no H1, returns
    (body, False) unchanged so render() can bail out.
    """
    lines = body.splitlines()

    h1_idx = -1
    for i, line in enumerate(lines):
        if line.startswith("# "):
            h1_idx = i
            break
        if line.strip():
            return body, False

    if h1_idx < 0:
        return body, False

    next_idx = len(lines)
    for j in range(h1_idx + 1, len(lines)):
        if lines[j].startswith("## "):
            next_idx = j
            break

    middle = lines[h1_idx + 1 : next_idx]
    middle = _strip_prior_headline_block(middle)
    # Trim leading blanks so render() can re-prefix cleanly.
    while middle and not middle[0].strip():
        middle.pop(0)

    rest = lines[next_idx:]
    out: list[str] = []
    if middle:
        out.extend(middle)
        if out[-1].strip():
            out.append("")
    out.extend(rest)
    return "\n".join(out).rstrip("\n"), True


_QUOTE_HEADLINE_RE = re.compile(r"^>\s+\*\*([^*]+)\*\*")
_QUOTE_NOW_RE = re.compile(r"^>\s+\*\*Now:\*\*")


def _strip_prior_headline_block(middle: list[str]) -> list[str]:
    """Remove a previously-injected quote+stats block from the H1→## window.

    Detection is shape-based: contiguous `>` quote lines that match the
    `> **<name>** ...` / `> **Now:** ...` pattern, plus an optional
    inline-code stats line on its own. Surrounding blanks are also
    consumed so the caller can re-insert cleanly.
    """
    if not middle:
        return middle

    i = 0
    while i < len(middle) and not middle[i].strip():
        i += 1

    start = i
    j = i
    saw_our_quote = False
    while j < len(middle) and middle[j].lstrip().startswith(">"):
        line = middle[j]
        if _QUOTE_HEADLINE_RE.match(line) or _QUOTE_NOW_RE.match(line):
            saw_our_quote = True
        j += 1

    if not saw_our_quote:
        return middle

    k = j
    while k < len(middle) and not middle[k].strip():
        k += 1

    if (
        k < len(middle)
        and middle[k].lstrip().startswith("`")
        and middle[k].rstrip().endswith("`")
    ):
        k += 1

    while k < len(middle) and not middle[k].strip():
        k += 1

    return middle[:start] + middle[k:]


def _count_section_bullets(body: str) -> dict[str, int]:
    """Count `- ` bullets under each `## Section` heading in the body."""
    counts: dict[str, int] = {}
    current: str | None = None
    for line in body.splitlines():
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            current = m.group(1).strip()
            counts[current] = 0
            continue
        if current and line.lstrip().startswith("- "):
            counts[current] = counts.get(current, 0) + 1
    return counts
