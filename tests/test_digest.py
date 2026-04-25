from __future__ import annotations

import json
from pathlib import Path

from sagent.digest import (
    _tool_summary,
    _truncate,
    build_timeline,
    compose_session_md,
)
from sagent.parser import Event, Session, load_session


def test_truncate_below_limit():
    assert _truncate("short", 200) == "short"


def test_truncate_collapses_whitespace():
    assert _truncate("a  b\n c", 200) == "a b c"


def test_truncate_above_limit():
    out = _truncate("x" * 500, 10)
    assert len(out) == 10
    assert out.endswith("…")


def test_tool_summary_edit():
    e = Event(
        kind="tool_use",
        uuid="",
        parent_uuid=None,
        timestamp=None,
        tool_name="Edit",
        tool_input={"file_path": "/a/b.py"},
    )
    assert _tool_summary(e) == "Edit(/a/b.py)"


def test_tool_summary_bash():
    e = Event(
        kind="tool_use",
        uuid="",
        parent_uuid=None,
        timestamp=None,
        tool_name="Bash",
        tool_input={"command": "echo hi"},
    )
    assert _tool_summary(e).startswith("Bash: echo hi")


def test_tool_summary_unknown_tool():
    e = Event(
        kind="tool_use",
        uuid="",
        parent_uuid=None,
        timestamp=None,
        tool_name="MysteryTool",
        tool_input={"foo": "bar"},
    )
    assert _tool_summary(e).startswith("MysteryTool(")


def _mini_session(tmp_path: Path) -> Session:
    p = tmp_path / "s.jsonl"
    records = [
        {
            "type": "user",
            "uuid": "u1",
            "parentUuid": None,
            "timestamp": "2026-04-22T10:00:00Z",
            "sessionId": "demo",
            "cwd": "/x",
            "gitBranch": "main",
            "message": {"role": "user", "content": [{"type": "text", "text": "build"}]},
        },
        {
            "type": "assistant",
            "uuid": "u2",
            "parentUuid": "u1",
            "timestamp": "2026-04-22T10:00:05Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "on it"},
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Write",
                        "input": {"file_path": "/x/a.py"},
                    },
                    {
                        "type": "tool_use",
                        "id": "t2",
                        "name": "Bash",
                        "input": {"command": "ls"},
                    },
                ],
            },
        },
    ]
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return load_session(p)


def test_build_timeline_contains_key_sections(tmp_path: Path):
    sess = _mini_session(tmp_path)
    out = build_timeline(sess)
    assert "# Timeline — `demo`" in out
    assert "**cwd:** `/x`" in out
    assert "**branch:** `main`" in out
    assert "## Tool usage" in out
    assert "`Write` × 1" in out
    assert "`Bash` × 1" in out
    assert "## Files written" in out
    assert "`/x/a.py`" in out
    assert "## Turn-by-turn" in out
    assert "### Turn 1" in out
    assert "**User:** build" in out
    assert "**Claude:** on it" in out


def test_compose_session_md_combines_sections(tmp_path: Path):
    sess = _mini_session(tmp_path)
    out = compose_session_md(
        sess,
        summary_md="# Summary\n\nUser asked to build something.",
        understanding_md="# Understanding\n\n## Decisions\n- Built it",
        project="src-x",
    )
    # front matter present
    assert out.startswith("---\n")
    assert 'type: "session"' in out
    assert 'project: "src-x"' in out
    assert 'source: "claude-code"' in out
    assert 'gist: "User asked to build something."' in out
    # body sections
    assert "# Session " in out
    assert "## Summary" in out
    assert "User asked to build something." in out
    assert "## Understanding" in out
    assert "Built it" in out
    # Timeline section is gone
    assert "## Timeline" not in out
