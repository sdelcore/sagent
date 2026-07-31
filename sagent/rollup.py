"""Project-level and scratchpad-level digests.

Two modes, auto-detected from the encoded Claude Code project name:
  - "scratchpad" — sessions originating from $HOME or /tmp; lots of one-offs,
    no overarching state. We produce only `recent.md` (text, no LLM).
  - "project" — everything else (e.g. -home-<user>-src-<name>). We produce
    `project.md` via incremental LLM roll-up that accumulates decisions,
    open threads, preferences, risks across sessions.

The shape of `project.md` itself — parse, render, headline, front-matter
derivation, stale decay, output validation, changelog diff — lives in
`project_doc.py`. This module owns the LLM calls, the scratchpad/index/groups
outputs, and file I/O.

`INDEX.md` and the advisory `GROUPS.md` are both written at index time, but
GROUPS.md needs a model: `update_index` refreshes it only when the caller
passes `groups_model`, so a `--no-llm` pass stays offline, and only once per
`groups_max_age_hours`, so a batch of sessions does not buy one grouping call
each.

Two rules shape the roll-up here. First, the LLM is the lossy component, so
every step that can be deterministic is: stale decay runs after the model
replies, never inside it. Second, a model reply that is not a digest must
never reach disk — one conversational answer already destroyed an accumulated
`project.md` and zeroed its counts, so the write is guarded and a rejected
reply raises `RollupRejected` instead of returning, which keeps the caller
from committing its rollup claim.
"""

from __future__ import annotations

import datetime as dt
import getpass
import re
import time
from pathlib import Path

from .frontmatter import split_front_matter, strip_front_matter, to_front_matter
from .llm import SECRETS_POLICY, query
from .project_context import read_project_context
from .project_doc import (
    DEFAULT_HARNESS,
    ProjectDoc,
    diff_front_matter,
    is_valid_rollup_output,
    merge_harnesses,
)
from .rate import RateLimiter
from .rebrand import git_remote_url


class RollupRejected(RuntimeError):
    """The roll-up reply was not a digest, so `project.md` was left alone.

    Raised rather than returned on purpose: the callers commit a rollup claim
    right after a successful call, and an exception is what makes them skip
    that commit and retry the project on the next pass.
    """


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
# Harness attribution
# ---------------------------------------------------------------------------


def session_harness(session_md: str) -> str:
    """The harness that produced a per-session digest.

    Digests written before opencode ingestion carry no `harness` field, so a
    missing value means Claude Code rather than "unknown".
    """
    fm, _ = split_front_matter(session_md)
    value = fm.get("harness") or fm.get("source") or ""
    return str(value).strip() or DEFAULT_HARNESS


def harnesses_in(session_files: list[Path]) -> list[str]:
    """Sorted harness names across a set of per-session digests."""
    return merge_harnesses(
        session_harness(_read_file(f)) for f in session_files
    )


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
    seen_harnesses: list[str] = []
    for f in session_files:
        # filename: 2026-04-22-abc12345.md
        m = re.match(r"^(\d{4}-\d{2}-\d{2})-([0-9a-f]+)\.md$", f.name)
        if not m:
            continue
        date, sid = m.group(1), m.group(2)
        body = f.read_text(errors="ignore")
        seen_harnesses.append(session_harness(body))
        time_match = re.search(r"started (\d{2}:\d{2})", body)
        hhmm = time_match.group(1) if time_match else ""
        gist = _extract_gist(body)
        by_date.setdefault(date, []).append((hhmm, sid, gist))

    out = project_dir / "recent.md"
    project_name = project_dir.name.lstrip("-")
    total = sum(len(v) for v in by_date.values())
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # A scratchpad collects one-offs from whatever harness ran in $HOME or
    # /tmp, so `source` cannot be assumed. Report what the digests say and
    # keep the scalar field for readers that predate the list.
    harnesses = merge_harnesses(seen_harnesses)
    fm = {
        "type": "scratchpad",
        "source": harnesses[0] if len(harnesses) == 1 else "mixed",
        "harnesses": harnesses,
        "project": project_name,
        "last_updated": now,
        "session_count_30d": total,
        "window_days": days,
    }

    lines: list[str] = [
        f"# {project_name} — recent",
        "",
        f"_last updated: {now} · {total} sessions in last {days} days_",
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

    out.write_text(to_front_matter(fm) + "\n" + "\n".join(lines))
    return out


def scan_digest_front_matter(out_root: Path) -> list[dict]:
    """Front matter of every `<project>/project.md` (or `recent.md`) below out_root.

    One entry per project directory, with `_dir` and `_path` added so a caller
    can build links. The index and the groups builder both need this view and
    must agree on which file speaks for a directory.
    """
    entries: list[dict] = []
    if not out_root.exists() or not out_root.is_dir():
        return entries
    for proj_dir in sorted(out_root.iterdir()):
        if not proj_dir.is_dir():
            continue
        for fname in ("project.md", "recent.md"):
            fp = proj_dir / fname
            if not fp.exists():
                continue
            fm, _ = split_front_matter(fp.read_text(errors="ignore"))
            if not fm:
                continue
            fm["_dir"] = proj_dir.name
            fm["_path"] = fname
            entries.append(fm)
            break  # one summary per project dir
    return entries


# Declared here because `update_index` owns the refresh gate; the file itself
# is written by `build_groups` further down.
GROUPS_FILENAME = "GROUPS.md"
GROUPS_MAX_AGE_HOURS = 24.0


def _groups_are_stale(out_root: Path, max_age_hours: float) -> bool:
    """True when GROUPS.md is worth spending an LLM call on.

    A missing file is always built, so the first digest pass on a host still
    produces the grouping. An existing one is left alone until it ages out,
    since the membership of a group changes over days, not over one session.
    A non-positive age forces the rebuild.
    """
    path = out_root / GROUPS_FILENAME
    if max_age_hours <= 0:
        return True
    try:
        age_hours = (time.time() - path.stat().st_mtime) / 3600.0
    except OSError:
        return True  # missing or unreadable — treat as never built
    return age_hours >= max_age_hours


def update_index(
    out_root: Path,
    *,
    groups_model: str | None = None,
    groups_max_age_hours: float = GROUPS_MAX_AGE_HOURS,
    rate_limiter: RateLimiter | None = None,
) -> Path | None:
    """Write a fleet-wide INDEX.md at out_root summarizing every project.

    Reads YAML front matter from each <project>/project.md or recent.md and
    builds a single-page overview with description/tagline/counts. The index
    itself is cheap — no LLM calls.

    GROUPS.md is refreshed here too, so the normal digest flow produces it,
    but only when `groups_model` is given: a run with the LLM disabled must
    pass None and stay offline. The refresh is also rate-gated by
    `groups_max_age_hours`, because the index runs after every single roll-up
    while the grouping pass costs one LLM call over the whole host — a batch
    of twenty sessions must not buy twenty near-identical groupings. Pass 0
    to force the refresh, which is what an explicit `--groups` asks for.
    """
    if not out_root.exists() or not out_root.is_dir():
        return None

    projects: list[dict] = []
    scratchpads: list[dict] = []
    for fm in scan_digest_front_matter(out_root):
        if fm.get("type") == "scratchpad":
            scratchpads.append(fm)
        else:
            projects.append(fm)

    out = out_root / "INDEX.md"
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    lines: list[str] = [
        f"# {out_root.name} — sagent index",
        "",
        f"_last updated: {now} · {len(projects)} project(s) · {len(scratchpads)} scratchpad(s)_",
        "",
    ]
    if projects:
        lines.append("## Projects")
        lines.append("")
        # newest activity first
        projects.sort(
            key=lambda p: str(p.get("last_updated", "")), reverse=True
        )
        for p in projects:
            name = p.get("project", p["_dir"])
            link = f"[[{p['_dir']}/project|{name}]]"
            count = p.get("session_count", "?")
            recent = p.get("sessions_last_7d", 0)
            recent_str = f", {recent} this week" if recent else ""
            tag = p.get("tagline", "")
            desc = p.get("description", "")
            lines.append(f"### {link}")
            if desc:
                lines.append(f"_{desc}_")
            if tag:
                lines.append(f"**now:** {tag}")
            stats_bits = [f"{count} sessions{recent_str}"]
            for fld, label in (
                ("decisions", "decisions"),
                ("open_threads", "open"),
                ("risks", "risks"),
            ):
                v = p.get(fld, 0) or 0
                if v:
                    stats_bits.append(f"{v} {label}")
            momentum = p.get("momentum")
            if momentum:
                stats_bits.append(f"momentum: {momentum}")
            lines.append("`" + " · ".join(stats_bits) + "`")
            lines.append("")
    if scratchpads:
        lines.append("## Scratchpads")
        lines.append("")
        scratchpads.sort(
            key=lambda p: str(p.get("last_updated", "")), reverse=True
        )
        for p in scratchpads:
            name = p.get("project", p["_dir"])
            link = f"[[{p['_dir']}/recent|{name}]]"
            count = p.get("session_count_30d", "?")
            window = p.get("window_days", 30)
            lines.append(f"- {link} · {count} sessions in last {window}d")
        lines.append("")

    out.write_text("\n".join(lines))

    if groups_model and _groups_are_stale(out_root, groups_max_age_hours):
        # Advisory output: a failure here must never cost the index.
        try:
            build_groups(out_root, model=groups_model, rate_limiter=rate_limiter)
        except Exception as exc:
            print(f"[sagent] GROUPS.md refresh failed for {out_root.name}: {exc}")

    return out


# ---------------------------------------------------------------------------
# GROUPS.md — advisory grouping of project keys, LLM-driven
# ---------------------------------------------------------------------------

GROUPS_NOTICE = (
    "ADVISORY ONLY. Nothing reads this file. No digest is merged, renamed, or "
    "rewritten because of it. It is a hint for a human deciding which project "
    "keys are really one effort."
)

GROUPS_PROMPT = SECRETS_POLICY + """You are grouping project digest keys for one machine.

Each key is one working directory on one host. The same effort often spans several keys — a service, its infrastructure, and its client can sit in three directories, and the same repository checked out on two hosts produces two keys. Your job is to name those groups.

The user message contains:
1. PRIOR GROUPS.md — the grouping in force right now (absent on the first run)
2. PROJECT LIST — every key on this host with its description and current tagline

Output ONLY markdown. No preamble, no commentary, no code fences.

For each group:

## <group name>
_<one line: why these keys are one effort>_
- `<project-key>` — <what this key contributes to the effort>

Then, last, a single section listing every key that stands alone:

## Ungrouped
- `<project-key>`

Rules:
- The PRIOR GROUPS.md is the current state. Keep every existing group, its name, and its members unless the PROJECT LIST now contradicts it. Stability matters more than elegance: a group that keeps being renamed is useless.
- Change an existing group only when the new material gives you a reason, and only in the smallest way that fits: add a key, drop a key that no longer belongs, or split a group whose members turned out to be separate efforts.
- Group only keys that are ONE effort. A shared language, a shared framework, or a shared tool is NOT a reason to group. Being the same repository, or being parts of one deployed system, is.
- A key belongs to at most one group. A group needs at least two keys.
- Never invent a key. Use only the keys given in the PROJECT LIST, spelled exactly.
- Every key in the PROJECT LIST appears exactly once in your output, either in a group or under Ungrouped."""


def build_groups(
    out_root: Path,
    *,
    model: str = "claude-haiku-4-5",
    rate_limiter: RateLimiter | None = None,
    max_projects: int = 200,
    max_chars: int = 24_000,
) -> Path | None:
    """Write the advisory `<out_root>/GROUPS.md`.

    One effort routinely spans several project keys — different cwds, or the
    same repo on two hosts — and no per-project digest can see that. This
    reads every digest's headline and asks the model which keys belong
    together.

    The prior GROUPS.md is fed back in as state, the same way the roll-up
    feeds back the prior project.md: without it the grouping is re-imagined
    on every run and the file churns. Returns None when there is nothing to
    group or the reply is empty, leaving any prior file untouched — the
    output is advisory, so no consumer is harmed by it being stale.
    """
    projects = [
        fm
        for fm in scan_digest_front_matter(out_root)
        if fm.get("type") != "scratchpad"
    ][:max_projects]
    if len(projects) < 2:
        return None

    lines: list[str] = []
    for fm in projects:
        key = str(fm.get("project") or fm["_dir"])
        desc = str(fm.get("description") or "").strip()
        tag = str(fm.get("tagline") or "").strip()
        entry = f"- `{key}`"
        if desc:
            entry += f" — {desc[:240]}"
        if tag:
            entry += f"\n    now: {tag[:200]}"
        lines.append(entry)
    listing = "\n".join(lines)
    if len(listing) > max_chars:
        listing = listing[:max_chars].rstrip() + "\n… [truncated]"

    out = out_root / GROUPS_FILENAME
    prior = strip_front_matter(_read_file(out)).strip()
    prior_section = (
        f"PRIOR GROUPS.md:\n\n{prior}\n\n---\n\n"
        if prior
        else "There is no prior GROUPS.md — this is the first grouping.\n\n"
    )

    text = query(
        GROUPS_PROMPT,
        f"Host: `{out_root.name}`\n\n{prior_section}PROJECT LIST:\n\n{listing}",
        model,
        rate_limiter=rate_limiter,
    ).strip()
    if not text:
        print(f"[sagent] GROUPS.md: empty reply for {out_root.name}, keeping prior")
        return None

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    fm = {
        "type": "groups",
        "advisory": True,
        "host": out_root.name,
        "project_count": len(projects),
        "last_updated": now,
    }
    header = (
        f"# {out_root.name} — project groups\n\n"
        f"_{GROUPS_NOTICE}_\n\n"
        f"_last updated: {now} · {len(projects)} project(s)_\n"
    )
    out.write_text(to_front_matter(fm) + "\n" + header + "\n" + text + "\n")
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


PROJECT_BASE_PROMPT = SECRETS_POLICY + """You are maintaining a cumulative project digest from a series of coding sessions. Your output is a single markdown document that a developer reads to catch up on what's been happening on this project across all sessions.

The user message may include a "PROJECT SOURCE CONTEXT" section that quotes the current state of files on disk (README, manifests, top-level entries, CLAUDE.md, etc.). This is authoritative for what the project IS — its purpose, tech stack, structure. The session transcripts are historical and may be out of date with what's on disk now. When deriving the description and "Current state" prose, prefer the source context for what the project IS; prefer the transcripts for what's been happening recently.

The user message may also include a "HARNESS MEMORY (NON-AUTHORITATIVE)" section. Its only permitted use is the cross-check described under HARNESS MEMORY RULES below.

Output ONLY the markdown document. Do not wrap it in code fences. No preamble, no commentary, no "here's the document". Never ask the reader a question and never explain what you did — if the source material is thin, emit the document anyway with whatever it supports.

The output MUST start with these two lines, in this exact format:

DESCRIPTION: <stable one-or-two-sentence description of what this project IS — what it's for, what it does. Less than 280 characters. Should not change much across sessions; describe the project, not the current state. Plain text, no quotes, no newlines.>
TAGLINE: <one-line headline about CURRENT STATE — what's actively in flight right now. May change every session. Plain text, no quotes, no newlines.>

Then a blank line, then the H1 and rest of the document.

Document body structure:

The first body line is the H1: `# <project name>`
Then these sections, in this exact order, with these exact heading spellings. Omit any section that would be empty — do not pad:

## Current state
10–20 sentences of running prose. What is this project? What's it for? What has happened recently? What's actively in flight right now? Concrete file names, commit hashes, decisions. Write for someone returning after a week — direct and specific.

## Recent activity
One bullet per recent session, newest first. Format: `- YYYY-MM-DD <id8> — one-line summary of what happened`. Up to ~10 entries.

## Invariants
Facts that do not change: architecture, language and runtime, deployment shape, naming and layout conventions, hard constraints the project works under. A reader may rely on these without checking. Format: `- <fact> (since YYYY-MM-DD)`.

## Current state - verify live
Facts that were true when a session recorded them but that a reader MUST re-check before relying on them: running versions, host and container names, branch and PR numbers, credential rotation state, counts, anything a later change can invalidate. Format: `- <fact> — verify: <how to check> (as of YYYY-MM-DD)`.

## Open threads
Work that's been started but not finished, questions raised but not answered. Format: `- <thing> (raised YYYY-MM-DD)`. Always keep a date on the bullet.

## Decisions
Permanent choices that shape ongoing work. Deduplicate across sessions. Cite the session that locked each in. Format: `- **<decision>** — <reason if known> (locked in YYYY-MM-DD)`.

## Resolved
Where finished threads and replaced facts go. This is the audit trail, so entries here are never removed or reworded once written.
Finished thread: `- **<thread>** — status: resolved — <how it ended> (YYYY-MM-DD)`
Replaced fact: `- **<old fact>** — status: superseded — superseded_by: "<what replaced it>" (YYYY-MM-DD)`

## Stale
Aged-out open threads. The tool maintains this section, not you. Copy any existing entries through verbatim. Never add to it and never remove from it.

## Contradictions
One entry each time the new material disagrees with what this document already says. Indent the three detail lines by four spaces:
- YYYY-MM-DD  <short label for the disputed fact>
    was:  "<what this document said>"
    now:  "<what the new material says>"
    src:  sessions/<YYYY-MM-DD-id8>
Existing entries are never removed or reworded.

## Preferences
Recurring style or process preferences that should influence future sessions. Deduplicate. Format: `- <preference> — <reason>`.

## Risks
Things flagged as risky, blocking, or fragile. Format: `- <risk> — <impact> (flagged YYYY-MM-DD)`.

FACT LIFECYCLE RULES — these override any habit of deleting:
- Nothing is ever deleted. A fact that stops being current is MOVED, never dropped, and its wording is preserved so the reader can see what was believed before.
- Each fact belongs under exactly one of `## Invariants` (cannot go out of date) or `## Current state - verify live` (can). When in doubt it is volatile — put it under `## Current state - verify live`.
- When the new material finishes an open thread, move that bullet to `## Resolved` with `status: resolved` and one clause saying how it ended.
- When the new material replaces or reverses a decision or a fact, write the new version in its own section AND move the old one to `## Resolved` with `status: superseded` and `superseded_by: "<what replaced it>"`.
- When the move happened because the new material DISAGREES with this document (rather than simply completing it), also append a `## Contradictions` entry. The contradiction entry is written in addition to the supersession, never instead of it.
- `src:` names where the new claim came from: `sessions/<YYYY-MM-DD-id8>` for a session digest, or `harness-memory` for the harness memory section.
- Carry every existing `## Resolved`, `## Stale`, and `## Contradictions` entry through unchanged.
- Deduplicating means merging two statements of the SAME fact. Two statements that disagree are not duplicates — supersede one and record the contradiction.

HARNESS MEMORY RULES:
- The harness memory section is NON-AUTHORITATIVE. It is another tool's notes about this project and it may be wrong, partial, or out of date.
- Use it for exactly one purpose: when it disagrees with this document, append a `## Contradictions` entry with `src:  harness-memory`.
- Do not copy facts out of it into any other section, do not rewrite any section from it, and never let it change the description or the tagline.

Rules:
- Be specific. Real file names, real decisions, real preferences.
- Don't invent. Only include what's actually in the source material.
- Where prior content is still accurate, preserve it word for word. Where new content changes state, add the new fact and move the old one.
- Every bullet outside `## Current state` and `## Recent activity` carries a `YYYY-MM-DD` date; the tool reads those dates to age facts out.
- No preamble, no meta-commentary, no code fences around the output."""

PROJECT_INCREMENTAL_SUFFIX = """

INCREMENTAL UPDATE MODE
The user message contains:
1. PRIOR PROJECT.md (full text)
2. NEW SESSION's per-session digest (summary + understanding sections)
3. optionally, PROJECT SOURCE CONTEXT and HARNESS MEMORY

Produce the full updated project.md. Start from the prior document and integrate the new session: add to Recent activity, fold new facts into Invariants / Current state - verify live / Open threads / Decisions / Preferences / Risks, apply the FACT LIFECYCLE RULES to anything the session resolves or contradicts, and update Current state if what's in flight changed. Reproduce every prior entry you did not explicitly move, including the whole `## Resolved`, `## Stale`, and `## Contradictions` sections. Output the complete document, not a patch."""


# ---------------------------------------------------------------------------
# Harness memory — cross-check material, never an input of record
# ---------------------------------------------------------------------------

HARNESS_MEMORY_CHAR_BUDGET = 6_000


def claude_projects_root() -> Path:
    """Where Claude Code keeps its per-project state. Resolved per call so a
    test (or a different `$HOME`) can redirect it."""
    return Path.home() / ".claude" / "projects"


def read_harness_memory(
    project_source_path: Path | str | None,
    *,
    max_chars: int = HARNESS_MEMORY_CHAR_BUDGET,
    projects_root: Path | None = None,
) -> str:
    """Concatenate the Claude Code memory files for a project's working tree.

    Claude Code writes durable notes to `<projects>/<cwd with / → ->/memory/*.md`.
    Those notes are a second opinion about the same project, so they are worth
    comparing against `project.md` — but they are maintained by another tool
    with its own retention rules, so they are only ever passed to the model as
    cross-check material. Opencode has no equivalent; the asymmetry is
    deliberate. Returns "" when the directory is absent, which is the common
    case, so callers never branch on it.
    """
    if not project_source_path:
        return ""
    root = projects_root or claude_projects_root()
    encoded = str(project_source_path).rstrip("/").replace("/", "-")
    memory_dir = root / encoded / "memory"
    try:
        files = sorted(memory_dir.glob("*.md"))
    except OSError:
        return ""

    blocks: list[str] = []
    total = 0
    for f in files:
        text = _read_file(f).strip()
        if not text:
            continue
        block = f"### {f.name}\n\n{text}"
        remaining = max_chars - total
        if remaining <= 0:
            break
        if len(block) > remaining:
            block = block[:remaining].rstrip() + "\n… [truncated]"
        blocks.append(block)
        total += len(block)
    return "\n\n".join(blocks)


def _memory_section(harness_memory_md: str) -> str:
    if not harness_memory_md.strip():
        return ""
    return (
        "HARNESS MEMORY (NON-AUTHORITATIVE — another tool's notes about this "
        "project; use it only to raise a `## Contradictions` entry with "
        "`src:  harness-memory`, never as a source of facts):\n\n"
        f"{harness_memory_md}\n\n---\n\n"
    )


def _context_section(project_context_md: str) -> str:
    if not project_context_md.strip():
        return ""
    return (
        "PROJECT SOURCE CONTEXT (current state of disk; authoritative for "
        f"what the project IS):\n\n{project_context_md}\n\n---\n\n"
    )


# Headings of the appendix sections `session_doc` writes after the prose:
# raw machine output kept for forensics, not for a reading model. The command
# block alone runs to its own 32_000-char budget: the worst real digest on
# this host is 3_374 chars of Summary + Understanding followed by 32_049
# chars of shell. Left in, it fills the whole session budget of this prompt
# and truncates the digest away.
#
# The prefixes are deliberately loose ("Commands" catches "Commands
# (verbatim)" and any later rename), and the rule is a deny-list rather than
# an allow-list because the Understanding body carries its own model-written
# "## Decisions" / "## Open threads" headings — keeping only known headings
# would cut the digest itself.
_APPENDIX_HEADING_RE = re.compile(r"^## (?:Commands|Timeline|Turn-by-turn)\b", re.M)


def _build_session_block(session_md: str, max_chars: int = 8_000) -> str:
    """Trim a session digest to fit token budget — keep summary + understanding,
    drop the verbatim appendix tail."""
    text = session_md
    m = _APPENDIX_HEADING_RE.search(text)
    if m and m.start() > 0:
        text = text[: m.start()].rstrip() + "\n"
    if len(text) > max_chars:
        text = text[: max_chars - 1] + "…"
    return text


def _run_project_rollup(
    *,
    project_name: str,
    prior_project_md: str,
    new_session_md: str,
    project_context_md: str,
    model: str,
    harness_memory_md: str = "",
    rate_limiter: RateLimiter | None = None,
) -> str:
    # Strip prior front matter before feeding to the LLM — the LLM should
    # only see the markdown body, and will emit a fresh DESCRIPTION/TAGLINE.
    prior_body = strip_front_matter(prior_project_md).strip()
    is_incremental = bool(prior_body)
    system = PROJECT_BASE_PROMPT + (
        PROJECT_INCREMENTAL_SUFFIX if is_incremental else ""
    )

    new_block = _build_session_block(new_session_md)

    ctx_section = _context_section(project_context_md)
    mem_section = _memory_section(harness_memory_md)

    if is_incremental:
        user = (
            f"Project: `{project_name}`\n\n"
            f"{ctx_section}"
            f"{mem_section}"
            f"PRIOR PROJECT.md:\n\n{prior_body}\n\n"
            f"---\n\n"
            f"NEW SESSION DIGEST:\n\n{new_block}"
        )
    else:
        user = (
            f"Project: `{project_name}`\n\n"
            f"{ctx_section}"
            f"{mem_section}"
            f"This is the first roll-up — no prior project.md exists.\n"
            f"Build the initial document from this single session digest:\n\n"
            f"{new_block}"
        )

    return query(system, user, model, rate_limiter=rate_limiter)


def _run_project_rebuild(
    *,
    project_name: str,
    session_files: list[Path],
    project_context_md: str,
    model: str,
    harness_memory_md: str = "",
    max_total_chars: int = 80_000,
    rate_limiter: RateLimiter | None = None,
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
        f"{_context_section(project_context_md)}"
        f"{_memory_section(harness_memory_md)}"
        f"Rebuild project.md from scratch using these session digests "
        f"(chronological, oldest first):\n"
        + "".join(blocks)
    )
    return query(system, user, model, rate_limiter=rate_limiter)


def roll_up_project(
    project_dir: Path,
    *,
    new_session_path: Path,
    project_source_path: Path | None = None,
    model: str = "claude-haiku-4-5",
    force_full: bool = False,
    full_rebuild_every: int = 10,
    rollup_count: int = 0,
    rate_limiter: RateLimiter | None = None,
    today: dt.date | None = None,
    harness_memory_root: Path | None = None,
) -> Path:
    """Update project.md after a new per-session digest landed.

    Cold start: no prior project.md → seed from this session.
    Incremental: pass prior + new → updated.
    Periodic full rebuild every Nth roll-up (or on force_full): re-derive
    project.md from all sessions in sessions/.

    Raises `RollupRejected` when the model answers with something that is not
    a digest. The prior project.md is left untouched in that case and no
    changelog entry is written, so the caller must not commit its rollup
    claim — the project is retried on the next pass.

    `today` and `harness_memory_root` exist so a test can pin the stale-decay
    clock and the memory directory.
    """
    project_md_path = project_dir / "project.md"
    sessions_dir = project_dir / "sessions"
    project_name = project_dir.name.lstrip("-")

    do_full_rebuild = force_full or (
        full_rebuild_every > 0
        and rollup_count > 0
        and (rollup_count + 1) % full_rebuild_every == 0
    )

    project_context_md = read_project_context(project_source_path)
    harness_memory_md = read_harness_memory(
        project_source_path, projects_root=harness_memory_root
    )

    # Read prior front matter BEFORE overwriting: it feeds the changelog diff
    # and carries the harness list forward. Pure derivation — no LLM call.
    prior = _read_file(project_md_path)
    prior_fm, _ = split_front_matter(prior)

    if do_full_rebuild and sessions_dir.exists():
        all_sessions = sorted(sessions_dir.glob("*.md"))
        harnesses = harnesses_in(all_sessions)
        text = _run_project_rebuild(
            project_name=project_name,
            session_files=all_sessions,
            project_context_md=project_context_md,
            model=model,
            harness_memory_md=harness_memory_md,
            rate_limiter=rate_limiter,
        )
    else:
        new_session_md = _read_file(new_session_path)
        # Read the harness of every digest on disk, not of this session
        # alone. A project.md written before opencode ingestion carries no
        # harness list, so trusting the new session plus the prior front
        # matter would hide the opencode digests already in sessions/ until
        # the next full rebuild. Cheap: file reads, no LLM call.
        on_disk = sorted(sessions_dir.glob("*.md")) if sessions_dir.exists() else []
        harnesses = merge_harnesses(
            [*harnesses_in(on_disk), session_harness(new_session_md)]
        )
        text = _run_project_rollup(
            project_name=project_name,
            prior_project_md=prior,
            new_session_md=new_session_md,
            project_context_md=project_context_md,
            model=model,
            harness_memory_md=harness_memory_md,
            rate_limiter=rate_limiter,
        )

    # The model sometimes replies conversationally instead of emitting the
    # document. Writing that reply destroys an accumulated digest, so refuse
    # the write and let the next pass try again.
    if not is_valid_rollup_output(text):
        print(
            f"[sagent] roll-up output rejected for {project_dir.name}: "
            f"no recognised '## ' section heading in {len(text.strip())} chars "
            f"of model output — keeping the prior project.md"
        )
        raise RollupRejected(f"unusable roll-up output for {project_dir.name}")

    doc = ProjectDoc.parse(text, name=project_name)

    # Age facts out deterministically. Decay must not depend on the model
    # noticing a date, and it runs before the counts are derived so the
    # front matter reports the post-decay shape.
    doc.apply_stale_decay(today=today)

    # Capture the live repo's remote so a later rename can be cross-walked.
    # If the source path is gone on this host, keep whatever we last saw
    # rather than nulling a previously-captured remote.
    remote_url = git_remote_url(project_source_path) or prior_fm.get("remote_url")

    fm = doc.derive_front_matter(
        sessions_dir=sessions_dir,
        remote_url=remote_url,
        harnesses=harnesses,
        prior_harnesses=prior_fm.get("harnesses"),
    )
    body = doc.render_body(front_matter=fm)

    delta_line = diff_front_matter(prior_fm, fm)
    if delta_line:
        _append_changelog_entry(project_dir, delta_line)

    project_md_path.write_text(to_front_matter(fm) + "\n" + body + "\n")
    return project_md_path


# ---------------------------------------------------------------------------
# Changelog file I/O — content of each line comes from project_doc.diff_*
# ---------------------------------------------------------------------------


def _append_changelog_entry(
    project_dir: Path, line: str, *, max_lines: int = 200
) -> Path:
    """Prepend `line` to <project_dir>/changelog.md, capped at `max_lines`."""
    changelog_path = project_dir / "changelog.md"
    project_name = project_dir.name.lstrip("-")
    header = f"# changelog — {project_name}"

    existing = _read_file(changelog_path)
    existing_entries: list[str] = []
    for raw in existing.splitlines():
        if raw.startswith("- "):
            existing_entries.append(raw)

    entries = [line, *existing_entries][:max_lines]

    text = header + "\n\n" + "\n".join(entries) + "\n"
    changelog_path.write_text(text)
    return changelog_path
