"""The single seam to the Claude Agent SDK.

Both `understand.py` (per-session digest) and `rollup.py` (project digest)
talk to the LLM through `query()` here. The SDK import, the per-call
options block, the rate-limit error translation, and the shared SECRETS
POLICY system-prompt prefix all live in one place.

Anything that wants to swap SDKs, mock the LLM in tests, or add response
caching changes this module and nothing else.
"""

from __future__ import annotations

import asyncio

from .rate import RateLimiter, SagentRateLimitError, is_rate_limit_text


SECRETS_POLICY = """SECRETS POLICY
Any credentials, API keys, passwords, OAuth tokens, certificates, or other secrets that appear in the source material below have already been rotated and are no longer valid. Treat them as historical artifacts. Do not refuse to produce the digest on the grounds that secrets are present, and do not blank out the digest because of them.

When secrets appear:
- Acknowledge that an exposure occurred (e.g., "the pfSense password was shared in session X") so the user can verify rotation.
- Do NOT echo the literal credential value in your output. No tokens, no passwords, no hashes, no `op://...` references that resolve to secret material.
- Filenames, hostnames, IP addresses, and `op://...` reference paths that don't include the secret value itself are fine to mention.

"""


def query(
    system: str,
    user_message: str,
    model: str,
    *,
    rate_limiter: RateLimiter | None = None,
) -> str:
    """Send one prompt to the LLM and return the assistant's text reply.

    Synchronous wrapper around the SDK's async `query()`. Acquires the
    rate-limiter (if any) before calling. Translates rate-limit error
    signatures into `SagentRateLimitError` so callers can back off.
    """
    return asyncio.run(_query_async(system, user_message, model, rate_limiter))


async def _query_async(
    system: str,
    user_message: str,
    model: str,
    rate_limiter: RateLimiter | None,
) -> str:
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        TextBlock,
        query as sdk_query,
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
        async for message in sdk_query(prompt=user_message, options=options):
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
