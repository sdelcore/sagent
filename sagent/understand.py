from __future__ import annotations

import asyncio

from .parser import Event, Session
from .rate import RateLimiter, SagentRateLimitError, is_rate_limit_text

UNDERSTANDING_BASE = """You are a scribe observing a coding session between a user and an AI coding agent (Claude Code). You read the session transcript and write a concise markdown digest.

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

UNDERSTANDING_INCREMENTAL_SUFFIX = """

INCREMENTAL UPDATE MODE
You are updating an existing digest. The user message contains:
1. The PRIOR SUMMARY (from the previous digest pass)
2. The PRIOR UNDERSTANDING (from the previous digest pass)
3. NEW EVENTS that occurred after the prior digest

Produce an updated full digest (Summary + Understanding) that:
- Preserves prior content that is still accurate.
- Updates or replaces prior content where new events change the state of things (e.g. an open thread was resolved, a decision was reversed, a new file was committed).
- Adds anything new from the new events.
- Removes prior content only if the new events contradict it; otherwise leave it.

Output the FULL updated documents in the same format as a fresh digest. Do not output a diff. Do not say "no changes" — always produce both documents."""


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


def build_transcript(
    events: list[Event],
    max_chars: int = 120_000,
    start_index: int = 0,
) -> str:
    blocks: list[str] = []
    total = 0
    for offset, e in enumerate(events):
        block = _render_event(e, start_index + offset)
        total += len(block)
        if total > max_chars:
            blocks.append(f"... [{len(events) - offset} further events truncated]")
            break
        blocks.append(block)
    return "\n\n".join(blocks)


async def _query_async(
    system: str,
    user_message: str,
    model: str,
    rate_limiter: RateLimiter | None = None,
) -> str:
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        TextBlock,
        query,
    )

    if rate_limiter is not None:
        rate_limiter.acquire()

    options = ClaudeAgentOptions(
        system_prompt=system,
        model=model,
        allowed_tools=[],
        max_turns=1,
        permission_mode="default",
        # Critical: prevent the SDK from writing our prompt as a new
        # ~/.claude/projects/<...>.jsonl session that the watcher would
        # then pick up and digest, recursing.
        extra_args={
            "no-session-persistence": None,
            "disable-slash-commands": None,
        },
    )

    text = ""
    final_result = ""
    try:
        async for message in query(prompt=user_message, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        text += block.text
            elif isinstance(message, ResultMessage):
                if message.result:
                    final_result = message.result
    except Exception as exc:
        if is_rate_limit_text(str(exc)):
            raise SagentRateLimitError(str(exc)) from exc
        raise
    return text or final_result


def _split_output(text: str) -> tuple[str, str]:
    sep = "---UNDERSTANDING---"
    if sep in text:
        summary, understanding = text.split(sep, 1)
    else:
        summary, understanding = text, ""
    return summary.strip() + "\n", understanding.strip() + "\n"


def run_understanding(
    session: Session,
    model: str = "claude-haiku-4-5",
    *,
    prior_summary: str = "",
    prior_understanding: str = "",
    since_event_index: int = 0,
    rate_limiter: RateLimiter | None = None,
) -> tuple[str, str]:
    """Returns (summary_md, understanding_md).

    If `prior_summary` is non-empty, runs in incremental mode: only events
    from `since_event_index` onward are sent to the LLM, along with the
    prior digest documents. Otherwise runs cold (full transcript).
    """
    is_incremental = bool(prior_summary.strip())
    new_events = session.events[since_event_index:] if is_incremental else session.events

    transcript = build_transcript(new_events, start_index=since_event_index)
    header = (
        f"Session `{session.session_id}` "
        f"(cwd: `{session.cwd}`, branch: `{session.git_branch}`)\n\n"
    )

    if is_incremental:
        system = UNDERSTANDING_BASE + UNDERSTANDING_INCREMENTAL_SUFFIX
        user_message = (
            f"{header}"
            f"PRIOR SUMMARY:\n{prior_summary.strip()}\n\n"
            f"PRIOR UNDERSTANDING:\n{prior_understanding.strip()}\n\n"
            f"NEW EVENTS (indices {since_event_index}–{len(session.events) - 1}):\n\n"
            f"{transcript}"
        )
    else:
        system = UNDERSTANDING_BASE
        user_message = f"{header}Transcript:\n\n{transcript}"

    text = asyncio.run(
        _query_async(system, user_message, model, rate_limiter=rate_limiter)
    )
    return _split_output(text)


