from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .frontmatter import to_front_matter
from .parser import Event, Session

# Sized from real sessions, not guessed: a busy Claude Code session yields ~460
# shell commands (~390 after dedup), and 8_000 kept only 32 of them — dropping
# 92% of the exact facts this block exists to preserve. 32_000 holds a full
# session. Opencode sessions are far smaller (~46 entries) and fit either way.
VERBATIM_CHAR_BUDGET = 32_000

_SHELL_TOOLS = {"bash"}
_WRITE_TOOLS = {
    "write",
    "edit",
    "multiedit",
    "notebookedit",
    "apply_patch",
    "patch",
}
_PATH_KEYS = (
    "file_path",
    "filePath",
    "notebook_path",
    "notebookPath",
    "path",
    "paths",
    "file_paths",
)

# opencode's apply_patch takes one patch document and no target path at all —
# its only input key is `patchText`. The files it writes are named in the
# patch's own headers, so without this the whole opencode write history is
# missing from the verbatim block.
_PATCH_KEYS = ("patchText", "patch", "diff", "patch_text")
_PATCH_TARGET_RE = re.compile(
    r"^\*\*\*\s+(?:Add|Update|Delete|Move)\s+File:\s*(.+?)\s*$", re.M
)

# opencode's bash tool takes a per-call working directory and varies it inside
# one session (measured: 608 bash parts over 13 directories, one session
# spanning three trees). Without it `git log` in two trees dedups to one
# ambiguous entry. Claude Code sends no such key, so its path is untouched.
_WORKDIR_KEYS = ("workdir", "cwd")


def _fmt_ts(ts: str | None) -> str:
    if not ts:
        return ""
    return ts.replace("T", " ").rstrip("Z").split(".")[0]


def _truncate(text: str, limit: int = 200) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _tool_summary(e: Event) -> str:
    name = e.tool_name or "?"
    inp = e.tool_input or {}
    if name in ("Edit", "Write", "Read", "NotebookEdit"):
        target = inp.get("file_path") or inp.get("notebook_path") or ""
        return f"{name}({target})"
    if name == "Bash":
        cmd = inp.get("command", "")
        return f"Bash: {_truncate(cmd, 100)}"
    if name == "Grep":
        return f"Grep({inp.get('pattern','')!r} in {inp.get('path','.')})"
    if name == "Glob":
        return f"Glob({inp.get('pattern','')})"
    if name in ("WebSearch", "WebFetch"):
        q = inp.get("query") or inp.get("url") or ""
        return f"{name}({_truncate(q, 100)})"
    if name == "TaskCreate":
        return f"TaskCreate: {inp.get('subject','')}"
    if name == "Agent":
        return f"Agent[{inp.get('subagent_type','general')}]: {inp.get('description','')}"
    return f"{name}({_truncate(str(inp), 80)})"


def build_timeline(session: Session) -> str:
    lines: list[str] = []
    lines.append(f"# Timeline — `{session.session_id}`")
    lines.append("")
    if session.cwd:
        lines.append(f"- **cwd:** `{session.cwd}`")
    if session.git_branch:
        lines.append(f"- **branch:** `{session.git_branch}`")
    lines.append(f"- **events:** {len(session.events)}")
    lines.append(f"- **user prompts:** {len(session.user_prompts)}")
    lines.append(f"- **tool calls:** {len(session.tool_uses)}")
    lines.append("")

    tool_counter: Counter[str] = Counter()
    for e in session.tool_uses:
        tool_counter[e.tool_name or "?"] += 1
    if tool_counter:
        lines.append("## Tool usage")
        lines.append("")
        for name, n in tool_counter.most_common():
            lines.append(f"- `{name}` × {n}")
        lines.append("")

    files_touched: Counter[str] = Counter()
    for e in session.tool_uses:
        if e.tool_name in ("Edit", "Write", "NotebookEdit"):
            p = (e.tool_input or {}).get("file_path") or (e.tool_input or {}).get(
                "notebook_path"
            )
            if p:
                files_touched[p] += 1
    if files_touched:
        lines.append("## Files written")
        lines.append("")
        for p, n in files_touched.most_common():
            lines.append(f"- `{p}` × {n}")
        lines.append("")

    lines.append("## Turn-by-turn")
    lines.append("")
    turn = 0
    for e in session.events:
        if e.kind == "user_prompt":
            turn += 1
            lines.append(f"### Turn {turn} — {_fmt_ts(e.timestamp)}")
            lines.append("")
            lines.append(f"**User:** {_truncate(e.text, 600)}")
            lines.append("")
        elif e.kind == "assistant_text":
            lines.append(f"**Claude:** {_truncate(e.text, 400)}")
            lines.append("")
        elif e.kind == "tool_use":
            lines.append(f"- `{_tool_summary(e)}`")
        elif e.kind == "tool_result" and e.is_error:
            lines.append(f"  - ⚠ error: {_truncate(e.text, 160)}")

    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class VerbatimEntry:
    """One exact fact: a shell command that ran, or a path that was written.

    `kind` separates the two populations, because a written path rendered
    beside shell commands reads as a command that would be executed. `workdir`
    is set only when the tool call named a directory other than the session
    cwd, so the common case renders exactly as before.
    """

    text: str
    kind: str = "command"
    workdir: str = ""


@dataclass
class VerbatimCommands:
    """Shell commands and written paths, deduped but never truncated.

    The stored `text` is the raw tool input, byte for byte. Nothing rewrites
    it. A secret typed into a session is already sitting in cleartext in the
    source JSONL on the same machine, so filtering the derived digest buys no
    protection while costing the exactness this block exists for — a pattern
    pass was tried and it both missed real credentials and destroyed real
    branch names.

    `command_calls` and `path_writes` keep the pre-dedup totals per population,
    because one summed total describes neither.
    """

    items: list[VerbatimEntry] = field(default_factory=list)
    command_calls: int = 0
    path_writes: int = 0

    @property
    def commands(self) -> list[VerbatimEntry]:
        return [i for i in self.items if i.kind == "command"]

    @property
    def paths(self) -> list[VerbatimEntry]:
        return [i for i in self.items if i.kind == "path"]

    @property
    def entries(self) -> list[str]:
        """Entry texts, commands first — for callers that need text only."""
        return [i.text for i in self.commands] + [i.text for i in self.paths]

    @property
    def raw_count(self) -> int:
        return self.command_calls + self.path_writes



def _path_values(inp: dict) -> list[str]:
    """Pull every plausible target path out of a write/edit tool input.

    Tool schemas differ per harness and per version, so read a candidate key
    set instead of trusting one name. Missing keys are normal, not an error.
    """
    out: list[str] = []
    for key in _PATH_KEYS:
        val = inp.get(key)
        if isinstance(val, str):
            if val.strip():
                out.append(val.strip())
        elif isinstance(val, (list, tuple)):
            out.extend(v.strip() for v in val if isinstance(v, str) and v.strip())
    return out


def patch_targets(inp: dict) -> list[str]:
    """Pull the target paths out of an apply_patch-style patch document.

    Public because the LLM transcript names the same targets in its compact
    tool signature, and both readers must agree on what a patch touched.
    """
    out: list[str] = []
    for key in _PATCH_KEYS:
        val = inp.get(key)
        if isinstance(val, str) and val:
            out.extend(m.strip() for m in _PATCH_TARGET_RE.findall(val))
    return [p for p in out if p]


def _workdir_for(inp: dict, session_cwd: str | None) -> str:
    """The tool call's own working directory, when it differs from the session.

    Returned empty for the session's own cwd so the common entry renders with
    no extra line, and empty for Claude Code, which sends no such key at all.
    """
    base = (session_cwd or "").rstrip("/")
    for key in _WORKDIR_KEYS:
        val = inp.get(key)
        if isinstance(val, str) and val.strip():
            wd = val.strip().rstrip("/")
            return "" if wd == base else wd
    return ""


def extract_verbatim(session: Session) -> VerbatimCommands:
    """Collect every shell command and written path, in first-seen order.

    The LLM digest is the lossy component of this pipeline, so this path holds
    no LLM and no truncation: an agent reading the digest later needs the exact
    command that ran, not a 100-char prefix of it. A command is therefore
    stored raw — leading and trailing whitespace included, because a heredoc
    terminator needs its newline — and `.strip()` decides emptiness only.

    Dedup keys include the per-call workdir, so the same command run in two
    trees stays two entries.
    """
    seen: set[tuple[str, str, str]] = set()
    items: list[VerbatimEntry] = []
    command_calls = 0
    path_writes = 0
    for e in session.events:
        if e.kind != "tool_use":
            continue
        name = (e.tool_name or "").lower()
        inp = e.tool_input if isinstance(e.tool_input, dict) else {}
        found: list[VerbatimEntry] = []
        if name in _SHELL_TOOLS:
            cmd = inp.get("command")
            if isinstance(cmd, str) and cmd.strip():
                found.append(
                    VerbatimEntry(
                        text=cmd,
                        kind="command",
                        workdir=_workdir_for(inp, session.cwd),
                    )
                )
        elif name in _WRITE_TOOLS:
            paths = _path_values(inp) + patch_targets(inp)
            found.extend(VerbatimEntry(text=p, kind="path") for p in paths)
        for item in found:
            if item.kind == "command":
                command_calls += 1
            else:
                path_writes += 1
            key = (item.kind, item.workdir, item.text)
            if key in seen:
                continue
            seen.add(key)
            items.append(item)
    return VerbatimCommands(
        items=items,
        command_calls=command_calls,
        path_writes=path_writes,
    )


def _plural(n: int, singular: str, plural: str | None = None) -> str:
    return f"{n} {singular if n == 1 else (plural or singular + 's')}"


def _fence_for(body: str) -> str:
    """Pick a fence longer than any backtick run inside the body."""
    longest = max((len(m) for m in re.findall(r"`+", body)), default=0)
    return "`" * max(3, longest + 1)


# Fence, newlines and an optional cwd line, so the budget covers what is
# actually written rather than the payload alone.
_ENTRY_OVERHEAD = 10


def _entry_cost(item: VerbatimEntry) -> int:
    if item.kind != "command":
        return len(item.text) + 5
    extra = len(item.workdir) + 10 if item.workdir else 0
    return len(item.text) + _ENTRY_OVERHEAD + extra


def _render_command(item: VerbatimEntry) -> list[str]:
    """One command as its own fenced block, with its workdir above it.

    One fence per entry, not one fence for all of them: a real command carries
    heredocs and embedded newlines, so a bare newline cannot delimit entries —
    a reader counting lines counts physical lines, not commands. A fence is the
    only delimiter that cannot collide with the command's own bytes (its width
    already widens past any backtick run), and it keeps every entry both
    byte-exact and separately copyable.
    """
    out: list[str] = []
    if item.workdir:
        out.append(f"_in `{item.workdir}`_")
    fence = _fence_for(item.text)
    out.append(fence)
    out.append(item.text)
    out.append(fence)
    out.append("")
    return out


def render_verbatim_block(
    verbatim: VerbatimCommands,
    *,
    budget: int = VERBATIM_CHAR_BUDGET,
) -> str:
    """Render the '## Commands (verbatim)' section, or '' when nothing ran.

    Entries are reproduced byte for byte. Nothing is filtered: the source
    JSONL already holds the same text in cleartext on this machine, so a
    redaction pass here would protect nothing while breaking the one promise
    this section makes.

    The first entry is always kept, even when it alone exceeds the budget: a
    block that cuts a command in half would defeat the point of the section.
    """
    if budget <= 0:
        return ""
    if not verbatim.items:
        return ""

    kept: list[VerbatimEntry] = []
    used = 0
    for item in verbatim.items:
        cost = _entry_cost(item)
        if kept and used + cost > budget:
            break
        kept.append(item)
        used += cost
    omitted = len(verbatim.items) - len(kept)

    shown_commands = [i for i in kept if i.kind == "command"]
    shown_paths = [i for i in kept if i.kind == "path"]
    all_commands = verbatim.commands
    all_paths = verbatim.paths

    lines: list[str] = ["## Commands (verbatim)", ""]
    if shown_commands:
        lines.append(
            f"### Shell commands ({len(shown_commands)} of {len(all_commands)} shown)"
        )
        lines.append("")
        for item in shown_commands:
            lines.extend(_render_command(item))
    if shown_paths:
        lines.append(
            f"### Files written ({len(shown_paths)} of {len(all_paths)} shown)"
        )
        lines.append("")
        # A list of code spans, never bare lines: a written path sitting among
        # shell commands reads as a command, and pasting it into a shell would
        # try to execute the file.
        lines.extend(f"- `{i.text}`" for i in shown_paths)
        lines.append("")

    bits: list[str] = []
    if verbatim.command_calls:
        bits.append(
            f"{_plural(verbatim.command_calls, 'bash call')} deduped to "
            f"{_plural(len(all_commands), 'command')}"
        )
    if verbatim.path_writes:
        bits.append(
            f"{_plural(verbatim.path_writes, 'write')} deduped to "
            f"{_plural(len(all_paths), 'path')}"
        )
    if omitted:
        bits.append(f"{omitted} omitted over budget")
    lines.append(f"_{' - '.join(bits)}_")
    lines.append("")
    return "\n".join(lines)


def _strip_top_heading(md: str, heading_starts: tuple[str, ...]) -> str:
    """Drop a leading top-level heading (e.g. '# Summary') if present."""
    text = md.strip()
    for h in heading_starts:
        if text.startswith(h):
            nl = text.find("\n")
            if nl >= 0:
                text = text[nl + 1 :]
            break
    return text.strip()


def _gist_from_summary(summary_md: str, max_chars: int = 200) -> str:
    """Extract the first sentence of the summary for the front-matter gist."""
    body = _strip_top_heading(summary_md, ("# Summary",))
    body = body.strip()
    if not body:
        return ""
    # First non-empty line, then first sentence-ish chunk
    line = next((l for l in body.splitlines() if l.strip()), "")
    line = line.strip().lstrip("#").strip()
    m = re.search(r"[.!?](\s|$)", line)
    if m:
        line = line[: m.start() + 1]
    if len(line) > max_chars:
        line = line[: max_chars - 1] + "…"
    return line


def compose_session_md(
    session: Session,
    *,
    summary_md: str,
    understanding_md: str,
    project: str,
    source: str = "claude-code",
    harness: str = "claude-code",
    verbatim_budget: int = VERBATIM_CHAR_BUDGET,
    timeline_md: str | None = None,  # accepted but ignored; kept for API stability
) -> str:
    """Combine summary + understanding into one document with YAML front matter.

    Timeline is no longer embedded — agents/humans wanting forensics should
    read the source JSONL referenced in front matter (`source_jsonl`).

    The verbatim commands block is appended deterministically, so it survives
    a degraded or skipped LLM digest, and it is reproduced byte for byte.
    """
    started_time = ""
    if session.started_at:
        try:
            started_time = session.started_at.split("T")[1][:5]
        except Exception:
            started_time = ""

    fm = {
        "type": "session",
        "source": source,
        "harness": harness,
        "session_id": session.session_id,
        "short_id": session.short_id,
        "date": session.date_prefix,
        "started_at": session.started_at or "",
        "project": project,
        "cwd": session.cwd or "",
        "branch": session.git_branch or "",
        "events": len(session.events),
        "prompts": len(session.user_prompts),
        "tools": len(session.tool_uses),
        "gist": _gist_from_summary(summary_md),
        "source_jsonl": str(session.path),
    }

    metadata_bits: list[str] = []
    if started_time:
        metadata_bits.append(f"started {started_time}")
    if session.cwd:
        metadata_bits.append(f"cwd: `{session.cwd}`")
    if session.git_branch:
        metadata_bits.append(f"branch: `{session.git_branch}`")
    metadata_bits.append(f"{len(session.events)} events")
    metadata_bits.append(f"{len(session.user_prompts)} prompts")
    metadata_bits.append(f"{len(session.tool_uses)} tool calls")

    body_parts = [
        f"# Session {session.short_id} — {session.date_prefix}",
        "",
        f"_{' · '.join(metadata_bits)}_",
        "",
        "## Summary",
        "",
        _strip_top_heading(summary_md, ("# Summary",)),
        "",
        "## Understanding",
        "",
        _strip_top_heading(understanding_md, ("# Understanding",)),
        "",
    ]
    block = render_verbatim_block(
        extract_verbatim(session), budget=verbatim_budget
    )
    if block:
        body_parts.append(block)
    return to_front_matter(fm) + "\n" + "\n".join(body_parts)


def write_session_md(
    session: Session,
    out_path: Path,
    *,
    harness: str = "claude-code",
    **kw,
) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(compose_session_md(session, harness=harness, **kw))
    return out_path
