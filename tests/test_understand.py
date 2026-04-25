from __future__ import annotations

from pathlib import Path

from sagent.parser import Event
from sagent.understand import (
    _brief_tool,
    _render_event,
    _strip_noise_tags,
    build_transcript,
)


def _evt(kind, **kw):
    defaults = dict(uuid="", parent_uuid=None, timestamp="2026-04-25T10:00:00Z")
    defaults.update(kw)
    return Event(kind=kind, **defaults)


def test_thinking_dropped():
    assert _render_event(_evt("assistant_thinking", text="long reasoning..."), 0) is None


def test_successful_tool_result_dropped():
    e = _evt("tool_result", text="huge file contents", is_error=False)
    assert _render_event(e, 0) is None


def test_error_tool_result_kept():
    e = _evt("tool_result", text="ENOENT no such file", is_error=True)
    out = _render_event(e, 0)
    assert out is not None
    assert "tool error" in out
    assert "ENOENT" in out


def test_user_prompt_kept_text_stripped():
    text = (
        "<local-command-caveat>Caveat: ignore me</local-command-caveat>\n"
        "what's the status of the build?"
    )
    out = _render_event(_evt("user_prompt", text=text), 0)
    assert out is not None
    assert "Caveat" not in out
    assert "what's the status" in out


def test_system_reminder_stripped():
    text = "<system-reminder>internal noise</system-reminder>actual question"
    cleaned = _strip_noise_tags(text)
    assert "system-reminder" not in cleaned
    assert "internal noise" not in cleaned
    assert "actual question" in cleaned


def test_bash_stdout_stripped():
    text = "I ran ls\n<bash-stdout>aaa\nbbb\nccc\n</bash-stdout>"
    cleaned = _strip_noise_tags(text)
    assert "bash-stdout" not in cleaned
    assert "aaa" not in cleaned
    assert "I ran ls" in cleaned


def test_user_prompt_only_noise_dropped():
    text = "<system-reminder>noise</system-reminder>"
    out = _render_event(_evt("user_prompt", text=text), 0)
    assert out is None


def test_brief_tool_edit():
    e = _evt("tool_use", tool_name="Edit", tool_input={"file_path": "/x/y.py"})
    assert _brief_tool(e) == "Edit /x/y.py"


def test_brief_tool_bash_truncates():
    long_cmd = "echo " + "x" * 200
    e = _evt("tool_use", tool_name="Bash", tool_input={"command": long_cmd})
    assert len(_brief_tool(e)) < 100  # truncated


def test_brief_tool_unknown_just_name():
    e = _evt("tool_use", tool_name="MysteryTool", tool_input={"foo": "bar"})
    assert _brief_tool(e) == "MysteryTool"


def test_tool_use_rendered_as_compact_signal():
    e = _evt(
        "tool_use",
        tool_name="Bash",
        tool_input={"command": "git status"},
    )
    out = _render_event(e, 7)
    assert out is not None
    assert "(tool: Bash:" in out
    assert "git status" in out


def test_build_transcript_filters_noise():
    events = [
        _evt("user_prompt", text="please build it"),
        _evt("assistant_thinking", text="hmm, let me think about how"),
        _evt("assistant_text", text="on it"),
        _evt(
            "tool_use",
            tool_name="Write",
            tool_input={"file_path": "/x.py", "content": "..." * 1000},
        ),
        _evt("tool_result", text="huge file content blob" * 100, is_error=False),
        _evt("assistant_text", text="done"),
    ]
    out = build_transcript(events)
    # thinking dropped
    assert "hmm, let me think" not in out
    # successful tool_result dropped
    assert "huge file content blob" not in out
    # tool_use kept but compact (no full content)
    assert "Write /x.py" in out
    assert "..." * 100 not in out
    # user + assistant text kept
    assert "please build it" in out
    assert "done" in out
