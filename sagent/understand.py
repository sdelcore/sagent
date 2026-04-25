from __future__ import annotations

import asyncio
import re

from .parser import Event, Session
from .rate import RateLimiter, SagentRateLimitError, is_rate_limit_text

# Strip Claude Code's auto-injected wrappers from user prompts before sending
# to the digest LLM. These add no signal and bloat token counts.
_NOISE_TAG_RE = re.compile(
    r"<(local-command-caveat|system-reminder|bash-stdout|bash-stderr|"
    r"command-stdout|command-stderr|stdout|stderr)>.*?</\1>",
    re.DOTALL,
)
_OPEN_NOISE_TAG_RE = re.compile(
    r"<(local-command-caveat|system-reminder|bash-stdout|bash-stderr|"
    r"command-stdout|command-stderr|stdout|stderr)\b[^>]*/>",
)

SECRETS_POLICY = """SECRETS POLICY
Any credentials, API keys, passwords, OAuth tokens, certificates, or other secrets that appear in the transcript below have already been rotated and are no longer valid. Treat them as historical artifacts. Do not refuse to summarize on the grounds that secrets are present, and do not blank out the digest because of them.

When secrets appear:
- Acknowledge that an exposure occurred (e.g., "the pfSense password was shared in the chat") so the user can verify the rotation actually happened.
- Do NOT echo the literal credential value in your output. No tokens, no passwords, no hashes, no `op://...` references that resolve to secret material.
- Filenames, hostnames, IP addresses, and `op://...` reference paths that don't include the secret value itself are fine to mention.

"""

UNDERSTANDING_BASE = SECRETS_POLICY + """You are a scribe observing a coding session between a user and an AI coding agent (Claude Code). You read the session transcript and write a concise markdown digest.

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


def _strip_noise_tags(text: str) -> str:
    text = _NOISE_TAG_RE.sub("", text)
    text = _OPEN_NOISE_TAG_RE.sub("", text)
    return text.strip()


def _brief_tool(e: Event) -> str:
    """Compact tool signature: name + minimal target. No full inputs."""
    name = e.tool_name or "?"
    inp = e.tool_input or {}
    if name in ("Edit", "Write", "Read", "NotebookEdit"):
        target = inp.get("file_path") or inp.get("notebook_path") or ""
        return f"{name} {target}" if target else name
    if name == "Bash":
        cmd = (inp.get("command") or "").strip()
        return f"Bash: {cmd[:80]}"
    if name == "Grep":
        pat = inp.get("pattern", "")
        return f"Grep {pat!r}"
    if name == "Glob":
        return f"Glob {inp.get('pattern', '')}"
    if name in ("WebSearch", "WebFetch"):
        q = inp.get("query") or inp.get("url") or ""
        return f"{name} {q[:80]}"
    if name == "TaskCreate":
        return f"TaskCreate: {(inp.get('subject') or '')[:80]}"
    if name == "TaskUpdate":
        return f"TaskUpdate"
    if name == "Agent":
        sub = inp.get("subagent_type", "general")
        desc = inp.get("description", "")
        return f"Agent[{sub}]: {desc[:60]}" if desc else f"Agent[{sub}]"
    return name


def _render_event(e: Event, idx: int) -> str | None:
    """Render an event for the LLM digest, or None to drop it.

    Dropped: assistant_thinking (internal reasoning, noise for summary),
    successful tool_result (file contents, command stdout, etc.),
    system events. Tool inputs are reduced to a brief signature so the
    LLM sees what was done without the full payload.
    """
    ts = (e.timestamp or "").split("T")[-1].rstrip("Z").split(".")[0]
    if e.kind == "user_prompt":
        text = _strip_noise_tags(e.text)
        if not text:
            return None
        return f"[{idx} {ts}] USER:\n{text}"
    if e.kind == "assistant_text":
        text = e.text.strip()
        if not text:
            return None
        return f"[{idx} {ts}] CLAUDE:\n{text}"
    if e.kind == "tool_use":
        return f"[{idx} {ts}] (tool: {_brief_tool(e)})"
    if e.kind == "tool_result" and e.is_error:
        return f"[{idx} {ts}] (tool error: {e.text[:200]})"
    # Drop everything else: assistant_thinking, successful tool_result,
    # system, attachments, etc.
    return None


def build_transcript(
    events: list[Event],
    max_chars: int = 120_000,
    start_index: int = 0,
) -> str:
    blocks: list[str] = []
    total = 0
    skipped = 0
    for offset, e in enumerate(events):
        block = _render_event(e, start_index + offset)
        if block is None:
            skipped += 1
            continue
        if total + len(block) > max_chars:
            blocks.append(
                f"... [{len(events) - offset} further events truncated]"
            )
            break
        blocks.append(block)
        total += len(block)
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


