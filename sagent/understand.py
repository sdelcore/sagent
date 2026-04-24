from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
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


def _run_via_sdk(system: str, user_message: str, model: str, max_tokens: int) -> str:
    """Run a single message via the Claude Agent SDK.

    The Agent SDK authenticates via (in priority order):
      1. ANTHROPIC_API_KEY env var (direct API)
      2. CLAUDE_CODE_OAUTH_TOKEN env var (from `claude setup-token`)
      3. Existing `~/.claude/` login state (same OAuth `claude login` uses)

    So this works on subscription hosts with zero key management — just
    needs `claude` on PATH (the SDK spawns it internally).
    """
    return asyncio.run(_run_via_sdk_async(system, user_message, model))


def _run_via_cli(system: str, user_message: str, model: str, timeout: int = 600) -> str:
    """Fall back to the `claude` CLI using the user's subscription auth.

    --no-session-persistence prevents the scribe's own call from landing in
    ~/.claude/projects — otherwise scribe watching its own project would pick
    up its own invocations and loop.
    """
    if not shutil.which("claude"):
        raise RuntimeError(
            "neither ANTHROPIC_API_KEY nor `claude` CLI is available"
        )
    cmd = [
        "claude",
        "-p",
        "--system-prompt", system,
        "--model", model,
        "--output-format", "text",
        "--no-session-persistence",
        "--disable-slash-commands",
        "--tools", "",
        "--permission-mode", "default",
    ]
    result = subprocess.run(
        cmd,
        input=user_message,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"claude CLI failed (exit {result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()[:500]}"
        )
    return result.stdout


def run_understanding(
    session: Session,
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 4096,
    backend: str = "auto",
) -> tuple[str, str]:
    """Returns (summary_md, understanding_md).

    backend:
      'auto' — use the Claude Agent SDK (handles subscription OAuth or
               ANTHROPIC_API_KEY transparently)
      'sdk'  — same as auto; explicit
      'cli'  — legacy raw `claude -p` subprocess path
    """
    transcript = build_transcript(session)
    user_message = (
        f"Session `{session.session_id}` "
        f"(cwd: `{session.cwd}`, branch: `{session.git_branch}`)\n\n"
        f"Transcript:\n\n{transcript}"
    )

    chosen = "sdk" if backend in ("auto", "sdk") else backend

    if chosen == "sdk":
        text = _run_via_sdk(UNDERSTANDING_SYSTEM, user_message, model, max_tokens)
    elif chosen == "cli":
        text = _run_via_cli(UNDERSTANDING_SYSTEM, user_message, model)
    else:
        raise ValueError(f"unknown backend: {backend}")

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
    backend: str = "auto",
) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    summary, understanding = run_understanding(session, model=model, backend=backend)
    summary_path = out_dir / "summary.md"
    understanding_path = out_dir / "understanding.md"
    summary_path.write_text(summary)
    understanding_path.write_text(understanding)
    return summary_path, understanding_path
