from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Literal

EventKind = Literal[
    "user_prompt",
    "assistant_thinking",
    "assistant_text",
    "tool_use",
    "tool_result",
    "system",
]

NOISE_TYPES = {"file-history-snapshot", "permission-mode", "last-prompt", "attachment"}


@dataclass
class Event:
    kind: EventKind
    uuid: str
    parent_uuid: str | None
    timestamp: str | None
    text: str = ""
    tool_name: str | None = None
    tool_input: dict[str, Any] | None = None
    tool_use_id: str | None = None
    is_error: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Session:
    session_id: str
    path: Path
    events: list[Event]
    cwd: str | None = None
    git_branch: str | None = None

    @property
    def user_prompts(self) -> list[Event]:
        return [e for e in self.events if e.kind == "user_prompt"]

    @property
    def tool_uses(self) -> list[Event]:
        return [e for e in self.events if e.kind == "tool_use"]

    @property
    def started_at(self) -> str | None:
        for e in self.events:
            if e.timestamp:
                return e.timestamp
        return None

    @property
    def date_prefix(self) -> str:
        ts = self.started_at
        if not ts:
            return "0000-00-00"
        return ts.split("T")[0]

    @property
    def short_id(self) -> str:
        return self.session_id.split("-")[0][:8]

    @property
    def is_sagent_self_generated(self) -> bool:
        """True if the first user prompt looks like one sagent emits.

        Used to skip sessions that were created by sagent's own LLM calls
        when the Agent SDK was persisting them — leftovers from before the
        --no-session-persistence flag landed.
        """
        prompts = self.user_prompts
        if not prompts:
            return False
        head = prompts[0].text.lstrip()
        markers = (
            "Session `",  # per-session digest prompt header
            "Project: `",  # project rollup prompt header
            "PRIOR SUMMARY:",  # incremental update marker
            "PRIOR PROJECT.md:",  # incremental rollup marker
        )
        return any(head.startswith(m) for m in markers)


def _content_blocks(msg: dict) -> list[dict]:
    c = msg.get("content")
    if isinstance(c, str):
        return [{"type": "text", "text": c}]
    if isinstance(c, list):
        return c
    return []


def _iter_records(path: Path) -> Iterator[dict]:
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def parse_record(rec: dict) -> Iterator[Event]:
    rtype = rec.get("type")
    if rtype in NOISE_TYPES:
        return
    uuid = rec.get("uuid", "")
    parent = rec.get("parentUuid")
    ts = rec.get("timestamp")

    if rtype == "user":
        msg = rec.get("message", {})
        for block in _content_blocks(msg):
            btype = block.get("type")
            if btype == "text":
                text = (block.get("text") or "").strip()
                if text:
                    yield Event("user_prompt", uuid, parent, ts, text=text, raw=rec)
            elif btype == "tool_result":
                content = block.get("content")
                if isinstance(content, list):
                    text = "".join(
                        p.get("text", "") for p in content if p.get("type") == "text"
                    )
                else:
                    text = str(content) if content else ""
                yield Event(
                    "tool_result",
                    uuid,
                    parent,
                    ts,
                    text=text,
                    tool_use_id=block.get("tool_use_id"),
                    is_error=bool(block.get("is_error")),
                    raw=rec,
                )

    elif rtype == "assistant":
        msg = rec.get("message", {})
        for block in _content_blocks(msg):
            btype = block.get("type")
            if btype == "thinking":
                text = (block.get("thinking") or "").strip()
                if text:
                    yield Event(
                        "assistant_thinking", uuid, parent, ts, text=text, raw=rec
                    )
            elif btype == "text":
                text = (block.get("text") or "").strip()
                if text:
                    yield Event("assistant_text", uuid, parent, ts, text=text, raw=rec)
            elif btype == "tool_use":
                yield Event(
                    "tool_use",
                    uuid,
                    parent,
                    ts,
                    tool_name=block.get("name"),
                    tool_input=block.get("input") or {},
                    tool_use_id=block.get("id"),
                    raw=rec,
                )

    elif rtype == "system":
        text = rec.get("content") or rec.get("text") or ""
        if isinstance(text, str) and text.strip():
            yield Event("system", uuid, parent, ts, text=text.strip(), raw=rec)


def load_session(path: str | Path) -> Session:
    path = Path(path)
    events: list[Event] = []
    cwd = None
    branch = None
    session_id = path.stem
    for rec in _iter_records(path):
        cwd = cwd or rec.get("cwd")
        branch = branch or rec.get("gitBranch")
        session_id = rec.get("sessionId", session_id)
        events.extend(parse_record(rec))
    return Session(
        session_id=session_id,
        path=path,
        events=events,
        cwd=cwd,
        git_branch=branch,
    )
