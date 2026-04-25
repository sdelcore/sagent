from __future__ import annotations

import getpass
from pathlib import Path

from sagent.rollup import (
    _count_section_bullets,
    _extract_description_tagline,
    _extract_gist,
    _first_sentence,
    is_scratchpad,
    update_index,
    update_recent,
)


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


def test_update_recent_emits_front_matter(tmp_path: Path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "2026-04-22-abc12345.md").write_text(
        "## Summary\n\nDebugged X.\n"
    )
    out = update_recent(tmp_path)
    text = out.read_text()
    assert text.startswith("---\n")
    assert 'type: "scratchpad"' in text
    assert 'project: "' in text
    assert "session_count_30d:" in text


def test_extract_description_tagline_both_present():
    body = (
        "DESCRIPTION: A project that does X.\n"
        "TAGLINE: Currently doing Y.\n"
        "\n"
        "# my project\n"
        "\n"
        "## Current state\n"
        "Going well.\n"
    )
    desc, tag, rest = _extract_description_tagline(body)
    assert desc == "A project that does X."
    assert tag == "Currently doing Y."
    assert rest.startswith("# my project")


def test_extract_description_tagline_caps_at_280():
    long_desc = "x" * 500
    body = f"DESCRIPTION: {long_desc}\nTAGLINE: short\n\n# proj\n"
    desc, _, _ = _extract_description_tagline(body)
    assert len(desc) <= 280


def test_extract_description_tagline_missing_lines_tolerated():
    body = "# heading\nbody\n"
    desc, tag, rest = _extract_description_tagline(body)
    assert desc == ""
    assert tag == ""
    assert rest.startswith("# heading")


def test_count_section_bullets():
    body = (
        "## Long-term decisions\n"
        "- a\n"
        "- b\n"
        "- c\n"
        "\n"
        "## Open threads\n"
        "- one\n"
        "\n"
        "## Risks & known issues\n"
    )
    counts = _count_section_bullets(body)
    assert counts["Long-term decisions"] == 3
    assert counts["Open threads"] == 1
    assert counts["Risks & known issues"] == 0


def test_update_index_lists_projects_and_scratchpads(tmp_path: Path):
    # Build a fake host output dir with one project and one scratchpad
    proj = tmp_path / "src-foo"
    proj.mkdir()
    (proj / "project.md").write_text(
        '---\n'
        'type: "project"\n'
        'project: "src-foo"\n'
        'description: "Foo project"\n'
        'tagline: "in flight"\n'
        'session_count: 4\n'
        'sessions_last_7d: 2\n'
        'decisions: 5\n'
        'open_threads: 1\n'
        'risks: 0\n'
        'last_updated: "2026-04-25T10:00:00Z"\n'
        '---\n'
        '# src-foo\n'
        '\n'
        'body...\n'
    )
    scratch = tmp_path / "home-user"
    scratch.mkdir()
    (scratch / "recent.md").write_text(
        '---\n'
        'type: "scratchpad"\n'
        'project: "home-user"\n'
        'session_count_30d: 47\n'
        'window_days: 30\n'
        'last_updated: "2026-04-25T10:00:00Z"\n'
        '---\n'
        '# home-user — recent\n'
    )

    out = update_index(tmp_path)
    assert out is not None
    text = out.read_text()
    assert "## Projects" in text
    assert "src-foo" in text
    assert "Foo project" in text
    assert "in flight" in text
    assert "## Scratchpads" in text
    assert "home-user" in text
    assert "47 sessions" in text
