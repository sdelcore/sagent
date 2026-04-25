from __future__ import annotations

import json
from pathlib import Path

from sagent.parser import NOISE_TYPES, load_session, parse_record


def _rec(**kw):
    return {"uuid": "u1", "parentUuid": None, "timestamp": "2026-04-22T00:00:00Z", **kw}


def test_noise_types_are_skipped():
    for t in NOISE_TYPES:
        assert list(parse_record(_rec(type=t))) == []


def test_user_text_prompt():
    rec = _rec(
        type="user",
        message={"role": "user", "content": [{"type": "text", "text": "hello"}]},
    )
    events = list(parse_record(rec))
    assert len(events) == 1
    assert events[0].kind == "user_prompt"
    assert events[0].text == "hello"


def test_user_string_content():
    rec = _rec(type="user", message={"role": "user", "content": "hi"})
    events = list(parse_record(rec))
    assert [e.kind for e in events] == ["user_prompt"]
    assert events[0].text == "hi"


def test_user_tool_result():
    rec = _rec(
        type="user",
        message={
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "t1",
                    "is_error": True,
                    "content": [{"type": "text", "text": "boom"}],
                }
            ],
        },
    )
    events = list(parse_record(rec))
    assert len(events) == 1
    e = events[0]
    assert e.kind == "tool_result"
    assert e.is_error is True
    assert e.tool_use_id == "t1"
    assert e.text == "boom"


def test_assistant_blocks_split():
    rec = _rec(
        type="assistant",
        message={
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "pondering"},
                {"type": "text", "text": "hello"},
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "Bash",
                    "input": {"command": "ls"},
                },
            ],
        },
    )
    kinds = [e.kind for e in parse_record(rec)]
    assert kinds == ["assistant_thinking", "assistant_text", "tool_use"]


def test_empty_text_is_skipped():
    rec = _rec(
        type="assistant",
        message={"role": "assistant", "content": [{"type": "text", "text": "   "}]},
    )
    assert list(parse_record(rec)) == []


def test_load_session(tmp_path: Path):
    p = tmp_path / "s.jsonl"
    records = [
        {"type": "permission-mode", "permissionMode": "default"},
        _rec(
            type="user",
            sessionId="abc",
            cwd="/tmp/x",
            gitBranch="main",
            message={"role": "user", "content": [{"type": "text", "text": "go"}]},
        ),
        _rec(
            type="assistant",
            uuid="u2",
            parentUuid="u1",
            message={
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Bash",
                        "input": {"command": "ls"},
                    }
                ],
            },
        ),
    ]
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n")

    sess = load_session(p)
    assert sess.session_id == "abc"
    assert sess.cwd == "/tmp/x"
    assert sess.git_branch == "main"
    assert len(sess.events) == 2
    assert sess.user_prompts[0].text == "go"
    assert sess.tool_uses[0].tool_name == "Bash"


def test_is_sagent_self_generated_per_session_prompt():
    from sagent.parser import Event, Session

    s = Session(
        session_id="x",
        path=Path("x"),
        events=[
            Event(
                kind="user_prompt",
                uuid="",
                parent_uuid=None,
                timestamp=None,
                text="Session `abc` (cwd: `/x`)\n\nTranscript:\n\n[0] USER:\nhi",
            )
        ],
    )
    assert s.is_sagent_self_generated


def test_is_sagent_self_generated_project_prompt():
    from sagent.parser import Event, Session

    s = Session(
        session_id="x",
        path=Path("x"),
        events=[
            Event(
                kind="user_prompt",
                uuid="",
                parent_uuid=None,
                timestamp=None,
                text="Project: `src-foo`\n\nThis is the first roll-up...",
            )
        ],
    )
    assert s.is_sagent_self_generated


def test_is_sagent_self_generated_negative():
    from sagent.parser import Event, Session

    s = Session(
        session_id="x",
        path=Path("x"),
        events=[
            Event(
                kind="user_prompt",
                uuid="",
                parent_uuid=None,
                timestamp=None,
                text="how do I configure systemd timer",
            )
        ],
    )
    assert not s.is_sagent_self_generated


def test_is_sagent_self_generated_empty():
    from sagent.parser import Session

    s = Session(session_id="x", path=Path("x"), events=[])
    assert not s.is_sagent_self_generated


def test_load_session_tolerates_bad_json(tmp_path: Path):
    p = tmp_path / "s.jsonl"
    p.write_text(
        "not-json\n"
        + json.dumps(
            _rec(
                type="user",
                message={"role": "user", "content": [{"type": "text", "text": "ok"}]},
            )
        )
        + "\n"
    )
    sess = load_session(p)
    assert len(sess.events) == 1
