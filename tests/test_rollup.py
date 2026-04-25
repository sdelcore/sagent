from __future__ import annotations

import getpass
from pathlib import Path

from sagent.rollup import _extract_gist, _first_sentence, is_scratchpad, update_recent


def test_is_scratchpad_user_home():
    user = getpass.getuser()
    assert is_scratchpad(f"-{user}")
    assert is_scratchpad(f"-home-{user}")


def test_is_scratchpad_tmp():
    assert is_scratchpad("-tmp")
    assert is_scratchpad("-var-tmp")


def test_is_scratchpad_real_project():
    user = getpass.getuser()
    assert not is_scratchpad(f"-home-{user}-src-droidcode")
    assert not is_scratchpad("-home-otheruser")


def test_first_sentence_basic():
    assert _first_sentence("hello world. and more.") == "hello world."


def test_first_sentence_strips_headings():
    assert _first_sentence("# Summary\n\nThe user asked. Then claude.") == "The user asked."


def test_first_sentence_truncates():
    long = "a" * 500
    assert _first_sentence(long, max_chars=20).endswith("…")
    assert len(_first_sentence(long, max_chars=20)) == 20


def test_extract_gist_from_session_md():
    md = """# Session abc12345

_metadata_

## Summary

The user debugged a Plex library scan issue.

## Understanding

## Decisions
- Fixed it
"""
    assert _extract_gist(md) == "The user debugged a Plex library scan issue."


def test_update_recent_writes_file(tmp_path: Path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "2026-04-22-abc12345.md").write_text(
        "# Session abc12345\n\n_started 14:32_\n\n"
        "## Summary\n\nDebugged X.\n\n## Understanding\n"
    )
    (sessions / "2026-04-23-def67890.md").write_text(
        "# Session def67890\n\n_started 09:15_\n\n"
        "## Summary\n\nReviewed Y.\n\n## Understanding\n"
    )
    out = update_recent(tmp_path)
    assert out.exists()
    text = out.read_text()
    assert "## 2026-04-23" in text
    assert "## 2026-04-22" in text
    # newer date should appear before older
    assert text.index("2026-04-23") < text.index("2026-04-22")
    assert "Debugged X." in text
    assert "Reviewed Y." in text
    assert "abc12345" in text
    assert "def67890" in text


def test_update_recent_handles_empty(tmp_path: Path):
    out = update_recent(tmp_path)
    # No sessions/ dir → no-op, returns the would-be path
    assert out == tmp_path / "recent.md"
    assert not out.exists()
