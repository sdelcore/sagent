"""Project-level and scratchpad-level digests.

Two modes, auto-detected from the encoded Claude Code project name:
  - "scratchpad" — sessions originating from $HOME or /tmp; lots of one-offs,
    no overarching state. We produce only `recent.md` (text, no LLM).
  - "project" — everything else (e.g. -home-<user>-src-<name>). We produce
    `project.md` via incremental LLM roll-up that accumulates decisions,
    open threads, preferences, risks across sessions.
"""

from __future__ import annotations

import asyncio
import getpass
import re
import time
from pathlib import Path

from .understand import _query_async  # type: ignore[reportPrivateUsage]


def _user() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return "user"


def is_scratchpad(project_dir_name: str) -> bool:
    """True if the encoded project name represents a non-project scratchpad.

    Matches:
      -<user>            → cwd was $HOME
      -tmp               → cwd was /tmp
    Anything else (e.g. -home-<user>-src-<x>) is treated as a real project.
    """
    user = _user()
    name = project_dir_name.lstrip("-")
    if name == user:
        return True
    if name.startswith("home-") and name == f"home-{user}":
        return True
    if name in ("tmp", "var-tmp"):
        return True
    return False


def _first_sentence(text: str, max_chars: int = 200) -> str:
    text = text.strip()
    if not text:
        return ""
    # strip leading "# Summary" type headings
    lines = [l for l in text.splitlines() if l.strip() and not l.startswith("#")]
    if not lines:
        return ""
    first = lines[0].strip()
    # take up to first period or max_chars
    m = re.search(r"[.!?](\s|$)", first)
    if m:
        first = first[: m.start() + 1]
    if len(first) > max_chars:
        first = first[: max_chars - 1] + "…"
    return first


def _read_file(p: Path) -> str:
    try:
        return p.read_text() if p.exists() else ""
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# Scratchpad recent.md — text only, no LLM
# ---------------------------------------------------------------------------


def update_recent(
    project_dir: Path,
    *,
    days: int = 30,
    max_sessions: int = 200,
) -> Path:
    """Generate recent.md for a scratchpad project. Text-only, no LLM call."""
    sessions_dir = project_dir / "sessions"
    if not sessions_dir.exists():
        return project_dir / "recent.md"

    # Files are <YYYY-MM-DD>-<id8>.md so name-sort is chronological.
    session_files = sorted(sessions_dir.glob("*.md"), reverse=True)[:max_sessions]

    cutoff = time.time() - days * 86_400
    session_files = [
        f for f in session_files if f.stat().st_mtime >= cutoff
    ] or session_files[:50]  # keep at least some if cutoff zeroes everything

    by_date: dict[str, list[tuple[str, str, str]]] = {}
    for f in session_files:
        # filename: 2026-04-22-abc12345.md
        m = re.match(r"^(\d{4}-\d{2}-\d{2})-([0-9a-f]+)\.md$", f.name)
        if not m:
            continue
        date, sid = m.group(1), m.group(2)
        body = f.read_text(errors="ignore")
        time_match = re.search(r"started (\d{2}:\d{2})", body)
        hhmm = time_match.group(1) if time_match else ""
        gist = _extract_gist(body)
        by_date.setdefault(date, []).append((hhmm, sid, gist))

    out = project_dir / "recent.md"
    project_name = project_dir.name.lstrip("-")
    lines: list[str] = [
        f"# {project_name} — recent",
        "",
        f"_last updated: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} · "
        f"{sum(len(v) for v in by_date.values())} sessions in last {days} days_",
        "",
    ]
    for date in sorted(by_date.keys(), reverse=True):
        lines.append(f"## {date}")
        for hhmm, sid, gist in sorted(by_date[date], reverse=True):
            link = f"[[sessions/{date}-{sid}|{sid}]]"
            prefix = f"{hhmm} " if hhmm else ""
            gist_str = f" — {gist}" if gist else ""
            lines.append(f"- {prefix}{link}{gist_str}")
        lines.append("")

    out.write_text("\n".join(lines))
    return out


def _extract_gist(session_md: str) -> str:
    """Pull a one-line gist from a per-session markdown file."""
    m = re.search(r"^## Summary\s*$\n+(.+?)(?=\n##|\Z)", session_md, re.M | re.S)
    if not m:
        return ""
    body = m.group(1).strip()
    return _first_sentence(body)


# ---------------------------------------------------------------------------
# Project project.md — LLM-driven incremental roll-up
# ---------------------------------------------------------------------------


PROJECT_BASE_PROMPT = """You are maintaining a cumulative project digest from a series of coding sessions. Your output is a single markdown document that a developer reads to catch up on what's been happening on this project across all sessions.

Output ONLY the markdown document. Do not wrap it in code fences. No preamble, no commentary, no "here's the document". Start with the heading.

Document structure:

The first line is the H1: `# <project name>`
The second line is a one-line italic metadata: session count, last updated date.
Then these sections, in order. Omit any section that would be empty — do not pad:

## Current state
10–20 sentences of running prose. What is this project? What's it for? What has happened recently? What's actively in flight right now? Concrete file names, commit hashes, decisions. Write for someone returning after a week — direct and specific.

## Recent activity
One bullet per recent session, newest first. Format: `- YYYY-MM-DD <id8> — one-line summary of what happened`. Up to ~10 entries.

## Long-term decisions
Permanent choices that shape ongoing work. Deduplicate across sessions. Cite the session that locked each in. Format: `- **<decision>** — <reason if known> (locked in <date>)`. Remove only if a later session reverses it.

## Open threads
Work that's been started but not finished, questions raised but not answered. Carry forward; remove only when a later session shows resolution. Format: `- <thing> (raised <date>)`.

## User preferences
Recurring style or process preferences that should influence future sessions. Deduplicate. Format: `- <preference> — <reason>`.

## Risks & known issues
Things flagged as risky, blocking, or fragile. Carry forward; remove on resolution.

Rules:
- Be specific. Real file names, real decisions, real preferences.
- Deduplicate aggressively. If the same preference recurs in multiple sessions, list it once.
- Don't invent. Only include what's actually in the source material.
- Where prior content is still accurate, preserve it. Where new content changes state, update.
- No preamble, no meta-commentary, no code fences around the output."""

PROJECT_INCREMENTAL_SUFFIX = """

INCREMENTAL UPDATE MODE
The user message contains:
1. PRIOR PROJECT.md (full text)
2. NEW SESSION's per-session digest (summary + understanding sections)

Produce the full updated project.md. Integrate the new session: add to Recent activity, fold relevant decisions/threads/preferences/risks into existing sections (deduplicating), update Current state if the new session changed what's in flight."""


def _build_session_block(session_md: str, max_chars: int = 8_000) -> str:
    """Trim a session digest to fit token budget — keep summary + understanding,
    drop the timeline tail."""
    text = session_md
    # Drop trailing "## Timeline" section if present
    timeline_at = text.find("\n## Timeline")
    if timeline_at > 0:
        text = text[:timeline_at]
    if len(text) > max_chars:
        text = text[: max_chars - 1] + "…"
    return text


async def _run_project_rollup_async(
    *,
    project_name: str,
    prior_project_md: str,
    new_session_md: str,
    model: str,
) -> str:
    is_incremental = bool(prior_project_md.strip())
    system = PROJECT_BASE_PROMPT + (
        PROJECT_INCREMENTAL_SUFFIX if is_incremental else ""
    )

    new_block = _build_session_block(new_session_md)

    if is_incremental:
        user = (
            f"Project: `{project_name}`\n\n"
            f"PRIOR PROJECT.md:\n\n{prior_project_md.strip()}\n\n"
            f"---\n\n"
            f"NEW SESSION DIGEST:\n\n{new_block}"
        )
    else:
        user = (
            f"Project: `{project_name}`\n\n"
            f"This is the first roll-up — no prior project.md exists.\n"
            f"Build the initial document from this single session digest:\n\n"
            f"{new_block}"
        )

    return await _query_async(system, user, model)


async def _run_project_rebuild_async(
    *,
    project_name: str,
    session_files: list[Path],
    model: str,
    max_total_chars: int = 80_000,
) -> str:
    """Full rebuild from many sessions in chronological order. Used to reset
    paraphrase drift every N roll-ups."""
    blocks: list[str] = []
    total = 0
    # iterate oldest → newest so the prompt reads chronologically
    for f in sorted(session_files):
        b = _build_session_block(f.read_text(errors="ignore"), max_chars=4_000)
        head = f"\n--- session {f.stem} ---\n\n"
        if total + len(b) + len(head) > max_total_chars:
            blocks.append(f"\n--- {len(session_files) - len(blocks)} earlier sessions truncated ---\n")
            break
        blocks.append(head + b)
        total += len(b) + len(head)

    system = PROJECT_BASE_PROMPT
    user = (
        f"Project: `{project_name}`\n\n"
        f"Rebuild project.md from scratch using these session digests "
        f"(chronological, oldest first):\n"
        + "".join(blocks)
    )
    return await _query_async(system, user, model)


def roll_up_project(
    project_dir: Path,
    *,
    new_session_path: Path,
    model: str = "claude-haiku-4-5",
    force_full: bool = False,
    full_rebuild_every: int = 10,
    rollup_count: int = 0,
) -> Path:
    """Update project.md after a new per-session digest landed.

    Cold start: no prior project.md → seed from this session.
    Incremental: pass prior + new → updated.
    Periodic full rebuild every Nth roll-up (or on force_full): re-derive
    project.md from all sessions in sessions/.
    """
    project_md_path = project_dir / "project.md"
    sessions_dir = project_dir / "sessions"
    project_name = project_dir.name.lstrip("-")

    do_full_rebuild = force_full or (
        full_rebuild_every > 0
        and rollup_count > 0
        and (rollup_count + 1) % full_rebuild_every == 0
    )

    if do_full_rebuild and sessions_dir.exists():
        all_sessions = sorted(sessions_dir.glob("*.md"))
        text = asyncio.run(
            _run_project_rebuild_async(
                project_name=project_name,
                session_files=all_sessions,
                model=model,
            )
        )
    else:
        prior = _read_file(project_md_path)
        new_session_md = _read_file(new_session_path)
        text = asyncio.run(
            _run_project_rollup_async(
                project_name=project_name,
                prior_project_md=prior,
                new_session_md=new_session_md,
                model=model,
            )
        )

    project_md_path.write_text(_strip_code_fence(text).strip() + "\n")
    return project_md_path


def _strip_code_fence(text: str) -> str:
    """Strip a wrapping ```...``` fence if the LLM took the prompt format
    literally and added one."""
    t = text.strip()
    if not t.startswith("```"):
        return text
    # remove opening fence (and optional language tag)
    nl = t.find("\n")
    if nl < 0:
        return text
    body = t[nl + 1 :]
    # remove closing fence at end if present
    if body.rstrip().endswith("```"):
        body = body.rstrip()[:-3]
    return body
