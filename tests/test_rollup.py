from __future__ import annotations

import getpass
import os
import time
from pathlib import Path

from sagent.rollup import (
    _append_changelog_entry,
    _build_project_front_matter,
    _count_section_bullets,
    _days_since_last_session,
    _extract_description_tagline,
    _extract_gist,
    _first_sentence,
    _format_changelog_line,
    _inject_headline_block,
    _momentum_bucket,
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


def _make_session(sessions_dir: Path, name: str, mtime: float) -> Path:
    """Create a fake session file with a specific mtime."""
    p = sessions_dir / name
    p.write_text("## Summary\n\nFake.\n")
    os.utime(p, (mtime, mtime))
    return p


def test_momentum_bucket_cold_when_no_recent_activity():
    assert _momentum_bucket(0, 0) == "cold"
    # cold dominates regardless of the prior window
    assert _momentum_bucket(0, 5) == "cold"


def test_momentum_bucket_cooling():
    assert _momentum_bucket(2, 5) == "cooling"
    assert _momentum_bucket(1, 2) == "cooling"


def test_momentum_bucket_steady():
    assert _momentum_bucket(3, 3) == "steady"
    assert _momentum_bucket(1, 1) == "steady"


def test_momentum_bucket_rising():
    assert _momentum_bucket(5, 2) == "rising"
    assert _momentum_bucket(1, 0) == "rising"


def test_days_since_last_session_empty():
    assert _days_since_last_session([], now=time.time()) is None


def test_days_since_last_session_basic(tmp_path: Path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    now = time.time()
    f1 = _make_session(sessions, "2026-04-20-aaaaaaaa.md", now - 5 * 86_400)
    f2 = _make_session(sessions, "2026-04-23-bbbbbbbb.md", now - 2 * 86_400)
    # Newest is f2 → 2 whole days since
    assert _days_since_last_session([f1, f2], now=now) == 2


def test_days_since_last_session_just_now_is_zero(tmp_path: Path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    now = time.time()
    f = _make_session(sessions, "2026-04-25-cccccccc.md", now - 60)
    assert _days_since_last_session([f], now=now) == 0


def test_build_project_front_matter_cold_when_empty(tmp_path: Path):
    fm = _build_project_front_matter(
        project_dir=tmp_path, body="", description="d", tagline="t"
    )
    assert fm["session_count"] == 0
    assert fm["sessions_last_7d"] == 0
    assert fm["days_since_last_session"] is None
    assert fm["momentum"] == "cold"


def test_build_project_front_matter_rising(tmp_path: Path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    now = time.time()
    # 3 sessions in last 7d, 1 in the prior 7d → rising
    _make_session(sessions, "2026-04-22-aaaaaaaa.md", now - 1 * 86_400)
    _make_session(sessions, "2026-04-22-bbbbbbbb.md", now - 3 * 86_400)
    _make_session(sessions, "2026-04-22-cccccccc.md", now - 6 * 86_400)
    _make_session(sessions, "2026-04-15-dddddddd.md", now - 10 * 86_400)
    fm = _build_project_front_matter(
        project_dir=tmp_path, body="", description="d", tagline="t"
    )
    assert fm["sessions_last_7d"] == 3
    assert fm["momentum"] == "rising"
    assert fm["days_since_last_session"] == 1


def test_build_project_front_matter_steady(tmp_path: Path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    now = time.time()
    # 2 in last 7d, 2 in prior 7d → steady
    _make_session(sessions, "s1.md", now - 1 * 86_400)
    _make_session(sessions, "s2.md", now - 4 * 86_400)
    _make_session(sessions, "s3.md", now - 9 * 86_400)
    _make_session(sessions, "s4.md", now - 12 * 86_400)
    fm = _build_project_front_matter(
        project_dir=tmp_path, body="", description="d", tagline="t"
    )
    assert fm["sessions_last_7d"] == 2
    assert fm["momentum"] == "steady"


def test_build_project_front_matter_cooling(tmp_path: Path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    now = time.time()
    # 1 in last 7d, 3 in prior 7d → cooling
    _make_session(sessions, "s1.md", now - 2 * 86_400)
    _make_session(sessions, "s2.md", now - 8 * 86_400)
    _make_session(sessions, "s3.md", now - 10 * 86_400)
    _make_session(sessions, "s4.md", now - 13 * 86_400)
    fm = _build_project_front_matter(
        project_dir=tmp_path, body="", description="d", tagline="t"
    )
    assert fm["sessions_last_7d"] == 1
    assert fm["momentum"] == "cooling"


def test_build_project_front_matter_cold_with_old_sessions(tmp_path: Path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    now = time.time()
    # 0 in last 7d, but old sessions exist → cold
    _make_session(sessions, "s1.md", now - 30 * 86_400)
    _make_session(sessions, "s2.md", now - 45 * 86_400)
    fm = _build_project_front_matter(
        project_dir=tmp_path, body="", description="d", tagline="t"
    )
    assert fm["sessions_last_7d"] == 0
    assert fm["momentum"] == "cold"
    assert fm["days_since_last_session"] == 30


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

def test_inject_headline_block_inserts_below_h1():
    body = "# aria\n\n## Current state\nGoing well.\n"
    fm = {
        "description": "Pushes Claude Code transcripts into Obsidian.",
        "tagline": "wiring up project.md headline injection",
        "decisions": 3,
        "open_threads": 2,
        "risks": 1,
    }
    out = _inject_headline_block(body, project_name="aria", fm=fm)
    lines = out.splitlines()
    # H1 still first
    assert lines[0] == "# aria"
    # Headline block sits before Current state
    assert "> **aria** — Pushes Claude Code transcripts into Obsidian." in out
    assert "> **Now:** wiring up project.md headline injection" in out
    assert "`3 decisions · 2 open · 1 risks`" in out
    # The injected block is above Current state
    assert out.index("> **aria**") < out.index("## Current state")
    # Trailing blank line before ## Current state preserved
    assert "\n\n## Current state" in out


def test_inject_headline_block_idempotent():
    body = "# aria\n\n## Current state\nGoing well.\n"
    fm = {
        "description": "A project.",
        "tagline": "moving",
        "decisions": 2,
        "open_threads": 0,
        "risks": 0,
    }
    once = _inject_headline_block(body, project_name="aria", fm=fm)
    twice = _inject_headline_block(once, project_name="aria", fm=fm)
    assert once == twice
    # And no duplicated quote lines
    assert once.count("> **aria** — A project.") == 1
    assert once.count("> **Now:** moving") == 1


def test_inject_headline_block_replaces_prior_block_with_new_data():
    # First inject with old data
    body = "# aria\n\n## Current state\nx\n"
    fm_old = {"description": "old desc", "tagline": "old tag", "decisions": 1}
    after_old = _inject_headline_block(body, project_name="aria", fm=fm_old)
    fm_new = {
        "description": "new desc",
        "tagline": "new tag",
        "decisions": 5,
        "open_threads": 1,
    }
    after_new = _inject_headline_block(after_old, project_name="aria", fm=fm_new)
    # Old strings gone, new strings present, no duplicate quote lines
    assert "old desc" not in after_new
    assert "old tag" not in after_new
    assert "new desc" in after_new
    assert "new tag" in after_new
    assert after_new.count("> **aria**") == 1
    assert "`5 decisions · 1 open`" in after_new


def test_inject_headline_block_omits_missing_stats_cleanly():
    # No counts set, no momentum, no days_since_last_session — issue #1 fields absent
    body = "# aria\n\n## Current state\nx\n"
    fm = {"description": "desc only", "tagline": "tag only"}
    out = _inject_headline_block(body, project_name="aria", fm=fm)
    assert "> **aria** — desc only" in out
    assert "> **Now:** tag only" in out
    # No stats line at all because every field is missing/zero
    assert "`" not in out
    assert "momentum" not in out
    assert "last session" not in out


def test_inject_headline_block_includes_momentum_and_days_when_present():
    body = "# aria\n\n## Current state\nx\n"
    fm = {
        "description": "desc",
        "tagline": "tag",
        "decisions": 4,
        "open_threads": 2,
        "risks": 1,
        "days_since_last_session": 3,
        "momentum": "hot",
    }
    out = _inject_headline_block(body, project_name="aria", fm=fm)
    assert (
        "`4 decisions · 2 open · 1 risks · last session 3d ago · momentum: hot`"
        in out
    )


def test_inject_headline_block_omits_missing_description_or_tagline():
    body = "# aria\n\n## Current state\nx\n"
    fm = {"decisions": 2}
    out = _inject_headline_block(body, project_name="aria", fm=fm)
    # Quote is project name only (no em dash + desc)
    assert "> **aria**\n" in out
    # No "Now:" line because tagline is empty
    assert "> **Now:**" not in out
    assert "`2 decisions`" in out


def test_inject_headline_block_no_h1_returns_body_unchanged():
    body = "no heading here\nsome text\n"
    out = _inject_headline_block(body, project_name="aria", fm={"description": "x"})
    assert out == body

def test_format_changelog_line_first_rollup_all_positive():
    new_fm = {
        "decisions": 2,
        "open_threads": 3,
        "preferences": 0,
        "risks": 1,
        "sessions_last_7d": 1,
        "last_updated": "2026-04-25T12:00:00Z",
    }
    line = _format_changelog_line({}, new_fm)
    assert line.startswith("- 2026-04-25T12:00:00Z — ")
    # Empty prior treated as zeros — every non-zero new count appears as +N
    assert "+2 decisions" in line
    assert "+3 open" in line  # label is "open"
    assert "+1 risk" in line
    # No "preferences" segment because new == prior == 0
    assert "preference" not in line
    assert "sessions_last_7d 0→1" in line


def test_format_changelog_line_field_order():
    prior = {
        "decisions": 0,
        "open_threads": 0,
        "preferences": 0,
        "risks": 0,
        "sessions_last_7d": 0,
    }
    new = {
        "decisions": 1,
        "open_threads": 1,
        "preferences": 1,
        "risks": 1,
        "sessions_last_7d": 1,
        "last_updated": "2026-04-25T12:00:00Z",
    }
    line = _format_changelog_line(prior, new)
    # decisions, open, preferences, risks — in that order
    i_dec = line.index("decision")
    i_open = line.index("open")
    i_pref = line.index("preference")
    i_risk = line.index("risk")
    assert i_dec < i_open < i_pref < i_risk


def test_format_changelog_line_only_changed_fields():
    prior = {
        "decisions": 5,
        "open_threads": 4,
        "preferences": 2,
        "risks": 1,
        "sessions_last_7d": 3,
    }
    new = {
        "decisions": 7,  # +2
        "open_threads": 1,  # -3
        "preferences": 2,  # unchanged
        "risks": 2,  # +1
        "sessions_last_7d": 3,  # unchanged
        "last_updated": "2026-04-25T12:00:00Z",
    }
    line = _format_changelog_line(prior, new)
    assert "+2 decisions" in line
    assert "-3 open" in line
    assert "+1 risk" in line
    # preferences unchanged → not in line
    assert "preference" not in line
    # sessions_last_7d unchanged → no "sessions_last_7d" segment
    assert "sessions_last_7d" not in line


def test_format_changelog_line_singular_plural():
    prior = {"decisions": 0, "open_threads": 0, "preferences": 0, "risks": 0}
    new = {
        "decisions": 1,
        "open_threads": 2,
        "preferences": 0,
        "risks": 0,
        "sessions_last_7d": 0,
        "last_updated": "2026-04-25T12:00:00Z",
    }
    line = _format_changelog_line(prior, new)
    assert "+1 decision" in line and "+1 decisions" not in line
    assert "+2 open" in line


def test_format_changelog_line_no_change_returns_empty():
    fm = {
        "decisions": 3,
        "open_threads": 2,
        "preferences": 1,
        "risks": 0,
        "sessions_last_7d": 4,
        "last_updated": "2026-04-25T12:00:00Z",
    }
    # Same on both sides → empty string
    assert _format_changelog_line(fm, fm) == ""


def test_format_changelog_line_momentum_only_when_present_and_changed():
    prior = {
        "decisions": 1,
        "open_threads": 1,
        "preferences": 0,
        "risks": 0,
        "sessions_last_7d": 1,
        "momentum": "cold",
    }
    new = {
        "decisions": 1,
        "open_threads": 1,
        "preferences": 0,
        "risks": 0,
        "sessions_last_7d": 1,
        "momentum": "rising",
        "last_updated": "2026-04-25T12:00:00Z",
    }
    line = _format_changelog_line(prior, new)
    assert "momentum cold→rising" in line

    # When momentum is missing from one side, no momentum segment
    line2 = _format_changelog_line(
        {**prior, "momentum": None}, {**new, "momentum": None}
    )
    assert line2 == ""  # nothing else changed either

    # When momentum is the same on both sides, no momentum segment
    same = {**prior, "momentum": "rising"}
    new_same = {**new, "momentum": "rising"}
    assert _format_changelog_line(same, new_same) == ""


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
        rollup, "_run_project_rollup_async",
        lambda **kw: _make_async(first_body),
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
        rollup, "_run_project_rollup_async",
        lambda **kw: _make_async(second_body),
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
        rollup, "_run_project_rollup_async",
        lambda **kw: _make_async(body),
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


async def _make_async(value: str) -> str:
    return value


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
