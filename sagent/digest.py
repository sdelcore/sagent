from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from .frontmatter import to_front_matter
from .parser import Event, Session


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
    timeline_md: str | None = None,  # accepted but ignored; kept for API stability
) -> str:
    """Combine summary + understanding into one document with YAML front matter.

    Timeline is no longer embedded — agents/humans wanting forensics should
    read the source JSONL referenced in front matter (`source_jsonl`).
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
    return to_front_matter(fm) + "\n" + "\n".join(body_parts)


def write_session_md(session: Session, out_path: Path, **kw) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(compose_session_md(session, **kw))
    return out_path
