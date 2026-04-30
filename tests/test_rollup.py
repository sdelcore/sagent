from __future__ import annotations

import getpass
from pathlib import Path

from sagent.rollup import (
    _append_changelog_entry,
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



def test_update_index_renders_momentum_badge(tmp_path: Path):
    proj = tmp_path / "src-bar"
    proj.mkdir()
    (proj / "project.md").write_text(
        '---\n'
        'type: "project"\n'
        'project: "src-bar"\n'
        'description: "Bar project"\n'
        'tagline: "going"\n'
        'session_count: 6\n'
        'sessions_last_7d: 0\n'
        'days_since_last_session: 12\n'
        'momentum: "cold"\n'
        'decisions: 6\n'
        'open_threads: 9\n'
        'risks: 0\n'
        'last_updated: "2026-04-25T10:00:00Z"\n'
        '---\n'
        '# src-bar\n'
    )
    out = update_index(tmp_path)
    assert out is not None
    text = out.read_text()
    assert "momentum: cold" in text


def test_append_changelog_entry_creates_and_prepends(tmp_path: Path):
    project_dir = tmp_path / "-home-user-src-aria"
    project_dir.mkdir()
    out = _append_changelog_entry(
        project_dir, "- 2026-04-22T08:31:00Z — +1 decision"
    )
    assert out.exists()
    text = out.read_text()
    assert text.startswith("# changelog — home-user-src-aria")
    assert "+1 decision" in text

    out = _append_changelog_entry(
        project_dir, "- 2026-04-25T12:00:00Z — +2 decisions"
    )
    text = out.read_text()
    # Newest first
    lines = [l for l in text.splitlines() if l.startswith("- ")]
    assert lines[0].startswith("- 2026-04-25T12:00:00Z")
    assert lines[1].startswith("- 2026-04-22T08:31:00Z")


def test_append_changelog_entry_truncates_at_max(tmp_path: Path):
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    # Seed an existing changelog with 250 entries (newest-first).
    header = "# changelog — proj"
    seeded = [f"- 2026-04-{(i % 28) + 1:02d}T00:00:00Z — +1 decision"
              for i in range(250)]
    (project_dir / "changelog.md").write_text(
        header + "\n\n" + "\n".join(seeded) + "\n"
    )
    _append_changelog_entry(
        project_dir, "- 2026-05-01T00:00:00Z — +9 decisions"
    )
    text = (project_dir / "changelog.md").read_text()
    entries = [l for l in text.splitlines() if l.startswith("- ")]
    assert len(entries) == 200
    # New entry is at the top
    assert entries[0].startswith("- 2026-05-01T00:00:00Z")


def test_roll_up_writes_changelog_when_counts_change(tmp_path: Path, monkeypatch):
    """Integration-style: stub the LLM and assert changelog is written/updated."""
    import sagent.rollup as rollup

    project_dir = tmp_path / "-home-user-src-aria"
    sessions = project_dir / "sessions"
    sessions.mkdir(parents=True)
    session_path = sessions / "2026-04-25-aaaaaaaa.md"
    session_path.write_text(
        "# Session aaaaaaaa\n\n## Summary\n\nDid X.\n\n## Understanding\n"
    )

    # First roll-up: pretend the LLM produced a body with 2 decisions
    # and 1 open thread.
    first_body = (
        "DESCRIPTION: An aria-like project.\n"
        "TAGLINE: building it.\n"
        "\n"
        "# aria\n"
        "\n"
        "## Long-term decisions\n"
        "- decision A\n"
        "- decision B\n"
        "\n"
        "## Open threads\n"
        "- thread 1\n"
    )
    monkeypatch.setattr(
        rollup, "_run_project_rollup",
        lambda **kw: first_body,
    )
    monkeypatch.setattr(
        rollup, "read_project_context", lambda p: ""
    )

    rollup.roll_up_project(project_dir, new_session_path=session_path)

    changelog = project_dir / "changelog.md"
    assert changelog.exists()
    text = changelog.read_text()
    assert text.startswith("# changelog — home-user-src-aria")
    assert "+2 decisions" in text
    assert "+1 open" in text
    initial_entries = [l for l in text.splitlines() if l.startswith("- ")]
    assert len(initial_entries) == 1

    # Second roll-up with changed counts: 3 decisions, 1 open thread, 1 risk.
    second_body = (
        "DESCRIPTION: An aria-like project.\n"
        "TAGLINE: still building.\n"
        "\n"
        "# aria\n"
        "\n"
        "## Long-term decisions\n"
        "- decision A\n"
        "- decision B\n"
        "- decision C\n"
        "\n"
        "## Open threads\n"
        "- thread 1\n"
        "\n"
        "## Risks & known issues\n"
        "- risk X\n"
    )
    monkeypatch.setattr(
        rollup, "_run_project_rollup",
        lambda **kw: second_body,
    )
    rollup.roll_up_project(project_dir, new_session_path=session_path)
    text = changelog.read_text()
    entries = [l for l in text.splitlines() if l.startswith("- ")]
    assert len(entries) == 2
    # Newest first → the +1 decision/+1 risk line is at top.
    assert "+1 decision" in entries[0]
    assert "+1 risk" in entries[0]


def test_roll_up_skips_changelog_when_counts_identical(
    tmp_path: Path, monkeypatch
):
    import sagent.rollup as rollup

    project_dir = tmp_path / "-home-user-src-aria"
    sessions = project_dir / "sessions"
    sessions.mkdir(parents=True)
    session_path = sessions / "2026-04-25-aaaaaaaa.md"
    session_path.write_text(
        "# Session aaaaaaaa\n\n## Summary\n\nDid X.\n\n## Understanding\n"
    )

    body = (
        "DESCRIPTION: project.\n"
        "TAGLINE: ongoing.\n"
        "\n"
        "# aria\n"
        "\n"
        "## Long-term decisions\n"
        "- a\n"
    )
    monkeypatch.setattr(
        rollup, "_run_project_rollup",
        lambda **kw: body,
    )
    monkeypatch.setattr(rollup, "read_project_context", lambda p: "")

    rollup.roll_up_project(project_dir, new_session_path=session_path)
    text_after_first = (project_dir / "changelog.md").read_text()
    entries_after_first = [
        l for l in text_after_first.splitlines() if l.startswith("- ")
    ]
    assert len(entries_after_first) == 1

    # Identical counts on next roll-up → no new entry should be added.
    rollup.roll_up_project(project_dir, new_session_path=session_path)
    text_after_second = (project_dir / "changelog.md").read_text()
    entries_after_second = [
        l for l in text_after_second.splitlines() if l.startswith("- ")
    ]
    assert len(entries_after_second) == 1


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
