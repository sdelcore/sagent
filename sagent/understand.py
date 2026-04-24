from __future__ import annotations

import asyncio
from pathlib import Path

from .parser import Event, Session

UNDERSTANDING_SYSTEM = """You are a scribe observing a coding session between a user and an AI coding agent (Claude Code). You read the session transcript and write a concise markdown digest.

You produce two documents in one response, separated by a line containing exactly `---UNDERSTANDING---`:

First, a `# Summary` document: 5–12 sentences of running prose describing what the session is about, what has happened, what the current state is, and what's in motion. Write for a human catching up after walking away — direct, specific, no filler. Include concrete file names, decisions, and blockers.

Then, after the separator, an `# Understanding` document with these sections (omit a section if truly empty — do not pad):

## Decisions
- Explicit choices the user or agent locked in. One bullet each, with the reason if stated.

## Open threads
- Questions raised but not resolved; work started but not finished; things the agent said it would do later.

## Ideas in passing
- Things the user mentioned offhand that weren't acted on but may matter later. Quote the user briefly.

## User preferences
- Guidance, style preferences, or corrections the user gave that should influence future behavior. Not project-specific code patterns — those live in the code.

## Risks & blockers
- Anything flagged as risky, uncertain, or blocking. Including the agent's own expressed uncertainty.

Rules:
- Be specific. Never write "various topics were discussed." Name them.
- Prefer direct quotes from the user for preferences and ideas. Quote briefly.
- Do not invent. If a section would be empty, omit its heading.
- Do not repeat the timeline. Extract signal.
- No preamble, no meta commentary about the document itself."""


def _render_event(e: Event, idx: int) -> str:
    ts = (e.timestamp or "").split("T")[-1].rstrip("Z").split(".")[0]
    if e.kind == "user_prompt":
        return f"[{idx} {ts}] USER:\n{e.text}"
    if e.kind == "assistant_text":
        return f"[{idx} {ts}] CLAUDE:\n{e.text}"
    if e.kind == "assistant_thinking":
        snippet = e.text[:600]
        return f"[{idx} {ts}] (thinking): {snippet}"
    if e.kind == "tool_use":
        inp_preview = str(e.tool_input)[:400] if e.tool_input else ""
        return f"[{idx} {ts}] TOOL {e.tool_name}: {inp_preview}"
    if e.kind == "tool_result":
        tag = "TOOL_ERROR" if e.is_error else "TOOL_RESULT"
        return f"[{idx} {ts}] {tag}: {e.text[:400]}"
    return f"[{idx} {ts}] {e.kind}: {e.text[:200]}"


def build_transcript(session: Session, max_chars: int = 120_000) -> str:
    blocks: list[str] = []
    total = 0
    for i, e in enumerate(session.events):
        block = _render_event(e, i)
        total += len(block)
        if total > max_chars:
            blocks.append(f"... [{len(session.events) - i} further events truncated]")
            break
        blocks.append(block)
    return "\n\n".join(blocks)


async def _run_via_sdk_async(system: str, user_message: str, model: str) -> str:
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        TextBlock,
        query,
    )

    options = ClaudeAgentOptions(
        system_prompt=system,
        model=model,
        allowed_tools=[],
        max_turns=1,
        permission_mode="default",
    )

    text = ""
    final_result = ""
    async for message in query(prompt=user_message, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    text += block.text
        elif isinstance(message, ResultMessage):
            if message.result:
                final_result = message.result
    return text or final_result


def run_understanding(
    session: Session,
    model: str = "claude-sonnet-4-6",
) -> tuple[str, str]:
    """Returns (summary_md, understanding_md).

    The Agent SDK authenticates via (in priority order): ANTHROPIC_API_KEY,
    CLAUDE_CODE_OAUTH_TOKEN, or the existing `~/.claude/` OAuth login.
    """
    transcript = build_transcript(session)
    user_message = (
        f"Session `{session.session_id}` "
        f"(cwd: `{session.cwd}`, branch: `{session.git_branch}`)\n\n"
        f"Transcript:\n\n{transcript}"
    )

    text = asyncio.run(_run_via_sdk_async(UNDERSTANDING_SYSTEM, user_message, model))

    sep = "---UNDERSTANDING---"
    if sep in text:
        summary, understanding = text.split(sep, 1)
    else:
        summary, understanding = text, ""
    return summary.strip() + "\n", understanding.strip() + "\n"


def write_understanding(
    session: Session,
    out_dir: Path,
    model: str = "claude-sonnet-4-6",
) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    summary, understanding = run_understanding(session, model=model)
    summary_path = out_dir / "summary.md"
    understanding_path = out_dir / "understanding.md"
    summary_path.write_text(summary)
    understanding_path.write_text(understanding)
    return summary_path, understanding_path
