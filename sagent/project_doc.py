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
  - the canonical set of sections, so a fact can be moved between them
    deterministically (stale decay) and so a structurally broken LLM reply
    can be rejected before it overwrites a good digest

The rollup module owns the LLM call, file I/O, and the changelog file
itself; this module owns the document type.
"""

from __future__ import annotations

import datetime as dt
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .frontmatter import cap_description


# ---------------------------------------------------------------------------
# Sections — the vocabulary of a project.md body
# ---------------------------------------------------------------------------

# Canonical order. A section is only ever created at its canonical slot
# (stale decay does this); existing sections are never reordered, because a
# user may hand-edit the file and re-shuffling their document is destructive.
CANONICAL_SECTIONS: tuple[str, ...] = (
    "Current state",
    "Recent activity",
    "Invariants",
    "Current state - verify live",
    "Open threads",
    "Decisions",
    "Resolved",
    "Stale",
    "Contradictions",
    "Preferences",
    "Risks",
)

# Heading spellings that map onto a canonical section. The LLM prompt has
# changed wording across versions ("Long-term decisions" → "Decisions") and
# older files on disk still carry the old headings, so recognition must be
# generous rather than exact.
_SECTION_ALIASES: dict[str, tuple[str, ...]] = {
    "Current state": ("current state",),
    "Recent activity": ("recent activity",),
    "Invariants": ("invariants",),
    "Current state - verify live": (
        "current state - verify live",
        "current state (verify live)",
        "verify live",
        "volatile",
    ),
    "Open threads": ("open threads", "open questions"),
    "Decisions": ("decisions", "long-term decisions", "key decisions"),
    "Resolved": ("resolved", "resolved threads"),
    "Stale": ("stale", "stale threads"),
    "Contradictions": ("contradictions",),
    "Preferences": ("preferences", "user preferences"),
    "Risks": (
        "risks",
        "risks & known issues",
        "risks & blockers",
        "known issues",
    ),
}

# Canonical section → front-matter count key. Sections that carry prose
# rather than facts (Current state, Recent activity) have no count.
_SECTION_COUNT_KEYS: dict[str, str] = {
    "Invariants": "invariants",
    "Current state - verify live": "volatile",
    "Open threads": "open_threads",
    "Decisions": "decisions",
    "Resolved": "resolved",
    "Stale": "stale",
    "Contradictions": "contradictions",
    "Preferences": "preferences",
    "Risks": "risks",
}

# Bullets left untouched for this long stop being "open" and become history.
STALE_AFTER_DAYS = 30

# A project.md with no harness recorded predates opencode ingestion, so it
# can only have come from Claude Code.
DEFAULT_HARNESS = "claude-code"

_H2_RE = re.compile(r"^##\s+(.+?)\s*$")
_BULLET_RE = re.compile(r"^-\s+")
_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def _normalise_heading(heading: str) -> str:
    """Fold a heading to a comparison key: lowercase, punctuation-free."""
    text = heading.strip().lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


_ALIAS_LOOKUP: dict[str, str] = {
    _normalise_heading(alias): canonical
    for canonical, aliases in _SECTION_ALIASES.items()
    for alias in (canonical, *aliases)
}


def canonical_section_name(heading: str) -> str | None:
    """Map a `## ` heading onto its canonical name, or None if unrecognised."""
    return _ALIAS_LOOKUP.get(_normalise_heading(heading))


def _canonical_rank(heading: str) -> int:
    """Sort position of a heading; unrecognised sections sort to the end."""
    canonical = canonical_section_name(heading)
    if canonical is None:
        return len(CANONICAL_SECTIONS)
    return CANONICAL_SECTIONS.index(canonical)


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
        """Count `- ` bullets under each `## Section` heading in `body`.

        Keys are the raw headings as written in the document.
        """
        return _count_section_bullets(self.body)

    def canonical_bullet_counts(self) -> dict[str, int]:
        """Bullet counts keyed by front-matter field, not by raw heading.

        Heading spellings drift ("Long-term decisions" vs "Decisions"), so
        counts for every alias of a canonical section are summed into one
        key. Sections carrying prose contribute nothing.
        """
        totals: dict[str, int] = dict.fromkeys(_SECTION_COUNT_KEYS.values(), 0)
        for heading, count in _count_section_bullets(self.body).items():
            canonical = canonical_section_name(heading)
            key = _SECTION_COUNT_KEYS.get(canonical or "")
            if key:
                totals[key] += count
        return totals

    def apply_stale_decay(
        self,
        *,
        today: dt.date | None = None,
        stale_after_days: int = STALE_AFTER_DAYS,
    ) -> int:
        """Move aged-out open threads into `## Stale`. Returns how many moved."""
        self.body, moved = decay_stale_threads(
            self.body, today=today, stale_after_days=stale_after_days
        )
        return moved

    def derive_front_matter(
        self,
        *,
        sessions_dir: Path,
        now: float | None = None,
        last_updated: str | None = None,
        remote_url: str | None = None,
        harnesses: Iterable[str] | str | None = None,
        prior_harnesses: Iterable[str] | str | None = None,
    ) -> dict[str, Any]:
        """Build the front-matter dict from doc fields and session stats.

        `sessions_dir` is the directory of per-session `.md` files; mtimes
        drive the recent-activity counts. `remote_url` is the project's git
        remote (captured by the caller) and is stored verbatim so rebrand
        detection can cross-walk renamed repos. `harnesses` are the harness
        names seen in this roll-up and `prior_harnesses` the ones already
        recorded in the file; they are unioned, never replaced, because one
        project.md accumulates sessions from several harnesses over time.
        `now` and `last_updated` are injectable for deterministic tests.
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

        counts = self.canonical_bullet_counts()

        days_since = _days_since_last_session(session_files, now=now)

        merged = merge_harnesses(prior_harnesses, harnesses)

        return {
            "type": "project",
            # The scalar predates the list and is kept for readers that only
            # know `source`. It must agree with the list, so it is derived
            # from it rather than assumed — a project whose sessions are all
            # opencode used to report `source: claude-code` beside
            # `harnesses: ["opencode"]`.
            "source": merged[0] if len(merged) == 1 else "mixed",
            "harnesses": merged,
            "project": self.name,
            "description": self.description,
            "tagline": self.tagline,
            "remote_url": remote_url,
            "last_updated": last_updated,
            "session_count": len(session_files),
            "sessions_last_7d": sessions_last_7d,
            "days_since_last_session": days_since,
            "momentum": decay_momentum(
                momentum_bucket(sessions_last_7d, sessions_prior_7d),
                days_since,
            ),
            "decisions": counts.get("decisions", 0),
            "open_threads": counts.get("open_threads", 0),
            "preferences": counts.get("preferences", 0),
            "risks": counts.get("risks", 0),
            "invariants": counts.get("invariants", 0),
            "volatile": counts.get("volatile", 0),
            "resolved": counts.get("resolved", 0),
            "stale": counts.get("stale", 0),
            "contradictions": counts.get("contradictions", 0),
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
# Harnesses — additive front-matter field
# ---------------------------------------------------------------------------


def merge_harnesses(*groups: Iterable[str] | str | None) -> list[str]:
    """Union harness names into one sorted list.

    Additive on purpose: a project.md that has ever seen an opencode session
    keeps `opencode` even when the next roll-up comes from Claude Code, so
    the field records the history of the file rather than the last writer.
    Accepts a bare string (front matter may hold a scalar) or any iterable.
    """
    names: set[str] = set()
    for group in groups:
        if group is None:
            continue
        items = [group] if isinstance(group, str) else list(group)
        for item in items:
            name = str(item).strip()
            if name:
                names.add(name)
    return sorted(names) or [DEFAULT_HARNESS]


# ---------------------------------------------------------------------------
# Stale decay — deterministic, no LLM
# ---------------------------------------------------------------------------


def decay_stale_threads(
    body: str,
    *,
    today: dt.date | None = None,
    stale_after_days: int = STALE_AFTER_DAYS,
) -> tuple[str, int]:
    """Move aged-out `## Open threads` bullets into `## Stale`.

    A thread nobody has touched for `stale_after_days` is no longer open in
    any useful sense, but deleting it loses evidence. Moving it is the
    fact-level mirror of project-level momentum decay: the bullet survives,
    it just stops competing for the reader's attention.

    A bullet ages by the last `YYYY-MM-DD` it carries (the `(raised <date>)`
    suffix the prompt asks for). A bullet with no parseable date stays put —
    guessing an age would be worse than leaving it open. Returns the new body
    and the number of bullets moved; the body is returned unchanged when
    nothing moved, so a re-run never churns the file.
    """
    if today is None:
        today = dt.date.today()

    preamble, sections = _split_sections(body)
    open_idx = _find_section(sections, "Open threads")
    if open_idx is None:
        return body, 0

    lead, blocks = _split_bullet_blocks(sections[open_idx].lines)
    kept: list[list[str]] = []
    moved: list[list[str]] = []
    for block in blocks:
        raised = _trailing_date("\n".join(block))
        if raised is not None and (today - raised).days > stale_after_days:
            moved.append(block)
        else:
            kept.append(block)

    if not moved:
        return body, 0

    sections[open_idx].lines = _rebuild_section_lines(lead, kept)

    stale_idx = _find_section(sections, "Stale")
    if stale_idx is None:
        stale = _Section(heading="Stale")
        stale_idx = _insert_at_canonical_slot(sections, stale)
    stale_lead, stale_blocks = _split_bullet_blocks(sections[stale_idx].lines)
    sections[stale_idx].lines = _rebuild_section_lines(
        stale_lead, [*stale_blocks, *moved]
    )

    return _join_sections(preamble, sections), len(moved)


# ---------------------------------------------------------------------------
# Roll-up output guard
# ---------------------------------------------------------------------------


def is_valid_rollup_output(text: str) -> bool:
    """True when an LLM roll-up reply is shaped like a project digest.

    The model sometimes answers conversationally instead of emitting the
    document ("The session digest you've provided is incomplete... Which
    would you prefer?"). Writing that reply destroys the accumulated digest
    and every count reads 0 afterwards. One recognised `## ` heading is
    enough to tell a document from a chat message, and it is a test the real
    output always passes. Pure — the caller decides what to do with False.
    """
    for line in _strip_code_fence(text).splitlines():
        m = _H2_RE.match(line)
        if m and canonical_section_name(m.group(1)) is not None:
            return True
    return False


# ---------------------------------------------------------------------------
# Section surgery helpers
# ---------------------------------------------------------------------------


@dataclass
class _Section:
    """One `## ` block: the heading text and every line under it."""

    heading: str
    lines: list[str] = field(default_factory=list)


def _split_sections(body: str) -> tuple[list[str], list[_Section]]:
    """Split a body into the pre-first-heading lines and its `## ` sections."""
    preamble: list[str] = []
    sections: list[_Section] = []
    for line in body.splitlines():
        m = _H2_RE.match(line)
        if m:
            sections.append(_Section(heading=m.group(1).strip()))
        elif sections:
            sections[-1].lines.append(line)
        else:
            preamble.append(line)
    return preamble, sections


def _join_sections(preamble: list[str], sections: list[_Section]) -> str:
    out = list(preamble)
    for section in sections:
        out.append(f"## {section.heading}")
        out.extend(section.lines)
    return "\n".join(out).rstrip("\n")


def _find_section(sections: list[_Section], canonical: str) -> int | None:
    for i, section in enumerate(sections):
        if canonical_section_name(section.heading) == canonical:
            return i
    return None


def _insert_at_canonical_slot(sections: list[_Section], new: _Section) -> int:
    """Insert `new` before the first section that outranks it; return its index."""
    rank = _canonical_rank(new.heading)
    index = len(sections)
    for i, section in enumerate(sections):
        if _canonical_rank(section.heading) > rank:
            index = i
            break
    sections.insert(index, new)
    if index > 0:
        prior = sections[index - 1].lines
        if prior and prior[-1].strip():
            prior.append("")
    return index


def _split_bullet_blocks(
    lines: list[str],
) -> tuple[list[str], list[list[str]]]:
    """Split section lines into a lead-in and one block per top-level bullet.

    A block owns its continuation lines (indented detail, `was:`/`now:`/`src:`),
    so moving a bullet moves its evidence with it.
    """
    lead: list[str] = []
    blocks: list[list[str]] = []
    for line in lines:
        if _BULLET_RE.match(line):
            blocks.append([line])
        elif blocks:
            blocks[-1].append(line)
        else:
            lead.append(line)
    return lead, blocks


def _rebuild_section_lines(
    lead: list[str], blocks: list[list[str]]
) -> list[str]:
    """Reassemble a section, normalising to one trailing blank line."""
    out = list(lead)
    for block in blocks:
        trimmed = list(block)
        while trimmed and not trimmed[-1].strip():
            trimmed.pop()
        out.extend(trimmed)
    while out and not out[-1].strip():
        out.pop()
    out.append("")
    return out


def _trailing_date(text: str) -> dt.date | None:
    """Last valid `YYYY-MM-DD` in `text`, or None when there is none."""
    for m in reversed(list(_DATE_RE.finditer(text))):
        try:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
    return None


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


def decay_momentum(momentum: str, days_since: int | None) -> str:
    """Override momentum so the surfaced value reflects calendar reality.

    The activity bucket alone goes stale: a `rising` value persists in
    front matter for weeks after the last session because nothing recomputes
    it. Anchor the surfaced value to recency instead:

    - < 7 days (or unknown):  keep the computed bucket
    - 7–14 days:              force `cooling`
    - >= 14 days:             force `cold`
    """
    if days_since is None:
        return momentum
    if days_since >= 14:
        return "cold"
    if days_since >= 7:
        return "cooling"
    return momentum


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
