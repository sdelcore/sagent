from __future__ import annotations

import getpass
from pathlib import Path

import pytest

from sagent.rollup import (
    _append_changelog_entry,
    _extract_gist,
    _first_sentence,
    GROUPS_FILENAME,
    GROUPS_NOTICE,
    RollupRejected,
    build_groups,
    is_scratchpad,
    read_harness_memory,
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


# ---------------------------------------------------------------------------
# Roll-up output guard (D13)
# ---------------------------------------------------------------------------

# The exact reply that overwrote workbox/hms-atlas/project.md and zeroed every
# count in it. This is the regression the guard exists for.
CONVERSATIONAL_REPLY = (
    "The session digest you've provided is incomplete - it shows the session "
    "just started (5 events, 1 tool call) and the Explore agent results "
    "haven't arrived yet.\n"
    "\n"
    "I can either wait for the completed session digest, or build the "
    "project.md from the little that is here.\n"
    "\n"
    "Which would you prefer?"
)

VALID_ROLLUP_OUTPUT = (
    "DESCRIPTION: The atlas service.\n"
    "TAGLINE: moving the deploy target.\n"
    "\n"
    "# hms-atlas\n"
    "\n"
    "## Current state\n"
    "Deploy target moved to Azure Container Apps.\n"
    "\n"
    "## Invariants\n"
    "- written in Python 3.12 (since 2026-01-04)\n"
    "\n"
    "## Decisions\n"
    "- **deploy to Azure Container Apps** (locked in 2026-07-28)\n"
    "\n"
    "## Contradictions\n"
    "- 2026-07-28  atlas deploy target\n"
    '    was:  "deploy from proxmox"\n'
    '    now:  "Azure Container Apps"\n'
    "    src:  sessions/2026-07-28-a1b2c3d4\n"
)

PRIOR_PROJECT_MD = (
    "---\n"
    'type: "project"\n'
    'project: "hms-atlas"\n'
    'description: "The atlas service."\n'
    'tagline: "running on proxmox"\n'
    "session_count: 12\n"
    "decisions: 3\n"
    "open_threads: 2\n"
    "preferences: 1\n"
    "risks: 1\n"
    'last_updated: "2026-07-20T10:00:00Z"\n'
    "---\n"
    "# hms-atlas\n"
    "\n"
    "## Current state\n"
    "Deployed from proxmox.\n"
    "\n"
    "## Decisions\n"
    "- **deploy from proxmox** (locked in 2026-03-01)\n"
    "- **key on the working directory** (locked in 2026-04-02)\n"
    "- **one digest per host** (locked in 2026-05-05)\n"
    "\n"
    "## Open threads\n"
    "- move off proxmox (raised 2026-07-19)\n"
    "- audit the token budget (raised 2026-07-20)\n"
    "\n"
    "## Preferences\n"
    "- short kebab-case branch names\n"
    "\n"
    "## Risks\n"
    "- the proxmox host is a single point of failure (flagged 2026-06-01)\n"
)


def _rollup_project(tmp_path: Path, *, prior: str | None = None) -> tuple[Path, Path]:
    """Build a project dir with one session digest and an optional prior digest."""
    project_dir = tmp_path / "hms-atlas"
    sessions = project_dir / "sessions"
    sessions.mkdir(parents=True)
    session_path = sessions / "2026-07-28-a1b2c3d4.md"
    session_path.write_text(
        "---\n"
        'type: "session"\n'
        'harness: "claude-code"\n'
        "---\n"
        "# Session a1b2c3d4\n\n## Summary\n\nStarted the deploy move.\n"
    )
    if prior is not None:
        (project_dir / "project.md").write_text(prior)
    return project_dir, session_path


def _stub_rollup(monkeypatch, reply: str) -> None:
    import sagent.rollup as rollup

    monkeypatch.setattr(rollup, "read_project_context", lambda p: "")
    monkeypatch.setattr(rollup, "git_remote_url", lambda p: None)
    monkeypatch.setattr(rollup, "_run_project_rollup", lambda **kw: reply)
    monkeypatch.setattr(rollup, "_run_project_rebuild", lambda **kw: reply)


def test_roll_up_rejects_the_conversational_reply(tmp_path: Path, monkeypatch):
    import sagent.rollup as rollup

    project_dir, session_path = _rollup_project(tmp_path, prior=PRIOR_PROJECT_MD)
    _stub_rollup(monkeypatch, CONVERSATIONAL_REPLY)

    with pytest.raises(RollupRejected):
        rollup.roll_up_project(project_dir, new_session_path=session_path)


def test_roll_up_rejection_leaves_the_prior_project_md_byte_identical(
    tmp_path: Path, monkeypatch
):
    import sagent.rollup as rollup

    project_dir, session_path = _rollup_project(tmp_path, prior=PRIOR_PROJECT_MD)
    _stub_rollup(monkeypatch, CONVERSATIONAL_REPLY)

    with pytest.raises(RollupRejected):
        rollup.roll_up_project(project_dir, new_session_path=session_path)

    assert (project_dir / "project.md").read_text() == PRIOR_PROJECT_MD


def test_roll_up_rejection_keeps_the_prior_counts_and_facts(
    tmp_path: Path, monkeypatch
):
    """The real failure zeroed every count. The prior evidence must survive."""
    import sagent.rollup as rollup

    project_dir, session_path = _rollup_project(tmp_path, prior=PRIOR_PROJECT_MD)
    _stub_rollup(monkeypatch, CONVERSATIONAL_REPLY)

    with pytest.raises(RollupRejected):
        rollup.roll_up_project(project_dir, new_session_path=session_path)

    from sagent.frontmatter import split_front_matter

    fm, body = split_front_matter((project_dir / "project.md").read_text())
    assert fm["decisions"] == 3
    assert fm["open_threads"] == 2
    assert "- **deploy from proxmox** (locked in 2026-03-01)" in body
    assert "Which would you prefer?" not in body


def test_roll_up_rejection_writes_no_changelog_entry(tmp_path: Path, monkeypatch):
    import sagent.rollup as rollup

    project_dir, session_path = _rollup_project(tmp_path, prior=PRIOR_PROJECT_MD)
    _stub_rollup(monkeypatch, CONVERSATIONAL_REPLY)

    with pytest.raises(RollupRejected):
        rollup.roll_up_project(project_dir, new_session_path=session_path)

    assert not (project_dir / "changelog.md").exists()


def test_roll_up_rejection_on_cold_start_writes_nothing(
    tmp_path: Path, monkeypatch
):
    import sagent.rollup as rollup

    project_dir, session_path = _rollup_project(tmp_path)
    _stub_rollup(monkeypatch, CONVERSATIONAL_REPLY)

    with pytest.raises(RollupRejected):
        rollup.roll_up_project(project_dir, new_session_path=session_path)

    assert not (project_dir / "project.md").exists()


def test_roll_up_rejection_guards_the_full_rebuild_path_too(
    tmp_path: Path, monkeypatch
):
    import sagent.rollup as rollup

    project_dir, session_path = _rollup_project(tmp_path, prior=PRIOR_PROJECT_MD)
    _stub_rollup(monkeypatch, CONVERSATIONAL_REPLY)

    with pytest.raises(RollupRejected):
        rollup.roll_up_project(
            project_dir, new_session_path=session_path, force_full=True
        )

    assert (project_dir / "project.md").read_text() == PRIOR_PROJECT_MD


def test_roll_up_accepts_a_well_formed_output(tmp_path: Path, monkeypatch):
    import sagent.rollup as rollup

    project_dir, session_path = _rollup_project(tmp_path, prior=PRIOR_PROJECT_MD)
    _stub_rollup(monkeypatch, VALID_ROLLUP_OUTPUT)

    out = rollup.roll_up_project(project_dir, new_session_path=session_path)

    text = out.read_text()
    assert "## Contradictions" in text
    assert "- **deploy to Azure Container Apps** (locked in 2026-07-28)" in text
    assert "deploy from proxmox" not in text.split("## Contradictions")[0]


def test_roll_up_accepted_output_records_the_new_section_counts(
    tmp_path: Path, monkeypatch
):
    import sagent.rollup as rollup
    from sagent.frontmatter import split_front_matter

    project_dir, session_path = _rollup_project(tmp_path, prior=PRIOR_PROJECT_MD)
    _stub_rollup(monkeypatch, VALID_ROLLUP_OUTPUT)

    out = rollup.roll_up_project(project_dir, new_session_path=session_path)
    fm, _ = split_front_matter(out.read_text())
    assert fm["invariants"] == 1
    assert fm["contradictions"] == 1
    assert fm["decisions"] == 1


def test_roll_up_applies_stale_decay_after_the_model_replies(
    tmp_path: Path, monkeypatch
):
    """Decay is deterministic, so it must not depend on the model noticing."""
    import datetime as dt

    import sagent.rollup as rollup
    from sagent.frontmatter import split_front_matter

    project_dir, session_path = _rollup_project(tmp_path)
    reply = (
        "DESCRIPTION: The atlas service.\n"
        "TAGLINE: still moving.\n"
        "\n"
        "# hms-atlas\n"
        "\n"
        "## Open threads\n"
        "- old thread (raised 2026-06-20)\n"
        "- fresh thread (raised 2026-07-25)\n"
    )
    _stub_rollup(monkeypatch, reply)

    out = rollup.roll_up_project(
        project_dir,
        new_session_path=session_path,
        today=dt.date(2026, 7, 30),
    )
    text = out.read_text()
    fm, body = split_front_matter(text)
    assert fm["open_threads"] == 1
    assert fm["stale"] == 1
    assert body.index("## Stale") < body.index("- old thread (raised 2026-06-20)")


def test_roll_up_carries_the_harness_list_forward(tmp_path: Path, monkeypatch):
    import sagent.rollup as rollup
    from sagent.frontmatter import split_front_matter

    prior = PRIOR_PROJECT_MD.replace(
        'type: "project"\n', 'type: "project"\nharnesses: ["opencode"]\n'
    )
    project_dir, session_path = _rollup_project(tmp_path, prior=prior)
    _stub_rollup(monkeypatch, VALID_ROLLUP_OUTPUT)

    out = rollup.roll_up_project(project_dir, new_session_path=session_path)
    fm, _ = split_front_matter(out.read_text())
    assert fm["harnesses"] == ["claude-code", "opencode"]


# ---------------------------------------------------------------------------
# Harness memory cross-check (D11)
# ---------------------------------------------------------------------------


def _seed_memory(root: Path, source: Path, name: str, text: str) -> Path:
    memory_dir = root / str(source).replace("/", "-") / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    f = memory_dir / name
    f.write_text(text)
    return f


def test_read_harness_memory_missing_dir_is_silent(tmp_path: Path):
    assert read_harness_memory("/home/user/src/aria", projects_root=tmp_path) == ""


def test_read_harness_memory_missing_projects_root_is_silent(tmp_path: Path):
    assert (
        read_harness_memory(
            "/home/user/src/aria", projects_root=tmp_path / "nope"
        )
        == ""
    )


def test_read_harness_memory_no_source_path_is_silent(tmp_path: Path):
    assert read_harness_memory(None, projects_root=tmp_path) == ""


def test_read_harness_memory_labels_each_file(tmp_path: Path):
    source = Path("/home/user/src/aria")
    _seed_memory(tmp_path, source, "architecture.md", "Deploys from proxmox.")
    _seed_memory(tmp_path, source, "conventions.md", "Branches are kebab-case.")
    text = read_harness_memory(source, projects_root=tmp_path)
    assert "### architecture.md" in text
    assert "### conventions.md" in text
    assert "Deploys from proxmox." in text
    assert "Branches are kebab-case." in text


def test_read_harness_memory_skips_empty_files(tmp_path: Path):
    source = Path("/home/user/src/aria")
    _seed_memory(tmp_path, source, "blank.md", "   \n\n")
    assert read_harness_memory(source, projects_root=tmp_path) == ""


def test_read_harness_memory_caps_the_char_budget(tmp_path: Path):
    source = Path("/home/user/src/aria")
    _seed_memory(tmp_path, source, "big.md", "x" * 5_000)
    text = read_harness_memory(source, projects_root=tmp_path, max_chars=200)
    assert len(text) <= 260
    assert "[truncated]" in text


def test_roll_up_passes_harness_memory_into_the_prompt(
    tmp_path: Path, monkeypatch
):
    import sagent.rollup as rollup

    project_dir, session_path = _rollup_project(tmp_path, prior=PRIOR_PROJECT_MD)
    source = Path("/home/user/src/hms-atlas")
    projects_root = tmp_path / "claude-projects"
    _seed_memory(projects_root, source, "notes.md", "Atlas deploys from proxmox.")

    captured: dict[str, str] = {}

    def fake_query(system, user, model, **kw):
        captured["system"] = system
        captured["user"] = user
        return VALID_ROLLUP_OUTPUT

    monkeypatch.setattr(rollup, "query", fake_query)
    monkeypatch.setattr(rollup, "read_project_context", lambda p: "")
    monkeypatch.setattr(rollup, "git_remote_url", lambda p: None)

    rollup.roll_up_project(
        project_dir,
        new_session_path=session_path,
        project_source_path=source,
        harness_memory_root=projects_root,
    )

    assert "HARNESS MEMORY (NON-AUTHORITATIVE" in captured["user"]
    assert "Atlas deploys from proxmox." in captured["user"]
    assert "harness-memory" in captured["system"]


def test_roll_up_omits_the_memory_section_when_the_dir_is_missing(
    tmp_path: Path, monkeypatch
):
    import sagent.rollup as rollup

    project_dir, session_path = _rollup_project(tmp_path, prior=PRIOR_PROJECT_MD)
    captured: dict[str, str] = {}

    def fake_query(system, user, model, **kw):
        captured["user"] = user
        return VALID_ROLLUP_OUTPUT

    monkeypatch.setattr(rollup, "query", fake_query)
    monkeypatch.setattr(rollup, "read_project_context", lambda p: "")
    monkeypatch.setattr(rollup, "git_remote_url", lambda p: None)

    out = rollup.roll_up_project(
        project_dir,
        new_session_path=session_path,
        project_source_path=Path("/home/user/src/hms-atlas"),
        harness_memory_root=tmp_path / "no-such-root",
    )

    assert "HARNESS MEMORY" not in captured["user"]
    assert out.exists()


# ---------------------------------------------------------------------------
# GROUPS.md — advisory grouping (D12)
# ---------------------------------------------------------------------------


GROUPS_REPLY = (
    "## hms\n"
    "_one deployed system split across three directories_\n"
    "- `hms-atlas` — the API\n"
    "- `hms-rag` — retrieval\n"
    "\n"
    "## Ungrouped\n"
    "- `src-sagent`\n"
)


def _seed_digest(
    root: Path,
    key: str,
    *,
    description: str = "",
    tagline: str = "",
    kind: str = "project",
) -> Path:
    d = root / key
    d.mkdir(parents=True, exist_ok=True)
    fname = "recent.md" if kind == "scratchpad" else "project.md"
    (d / fname).write_text(
        "---\n"
        f'type: "{kind}"\n'
        f'project: "{key}"\n'
        f'description: "{description}"\n'
        f'tagline: "{tagline}"\n'
        'last_updated: "2026-07-28T10:00:00Z"\n'
        "---\n"
        f"# {key}\n"
    )
    return d / fname


def _seed_three_projects(root: Path) -> None:
    _seed_digest(root, "hms-atlas", description="The atlas API.", tagline="deploy")
    _seed_digest(root, "hms-rag", description="Retrieval for atlas.", tagline="index")
    _seed_digest(root, "src-sagent", description="Session digests.", tagline="tests")


def test_build_groups_writes_the_advisory_marker(tmp_path: Path, monkeypatch):
    import sagent.rollup as rollup

    _seed_three_projects(tmp_path)
    monkeypatch.setattr(rollup, "query", lambda *a, **kw: GROUPS_REPLY)

    out = build_groups(tmp_path)
    assert out == tmp_path / GROUPS_FILENAME
    text = out.read_text()
    assert "advisory: true" in text
    assert GROUPS_NOTICE in text
    assert "ADVISORY ONLY" in text


def test_build_groups_writes_the_model_reply_verbatim(tmp_path: Path, monkeypatch):
    import sagent.rollup as rollup

    _seed_three_projects(tmp_path)
    monkeypatch.setattr(rollup, "query", lambda *a, **kw: GROUPS_REPLY)

    text = build_groups(tmp_path).read_text()
    assert "## hms" in text
    assert "- `hms-atlas` — the API" in text
    assert "## Ungrouped" in text


def test_build_groups_lists_every_project_key_in_the_prompt(
    tmp_path: Path, monkeypatch
):
    import sagent.rollup as rollup

    _seed_three_projects(tmp_path)
    captured: dict[str, str] = {}

    def fake_query(system, user, model, **kw):
        captured["system"] = system
        captured["user"] = user
        return GROUPS_REPLY

    monkeypatch.setattr(rollup, "query", fake_query)
    build_groups(tmp_path)

    assert "PROJECT LIST:" in captured["user"]
    for key in ("hms-atlas", "hms-rag", "src-sagent"):
        assert f"`{key}`" in captured["user"]
    assert "The atlas API." in captured["user"]
    assert "now: deploy" in captured["user"]


def test_build_groups_first_run_says_there_is_no_prior(tmp_path: Path, monkeypatch):
    import sagent.rollup as rollup

    _seed_three_projects(tmp_path)
    captured: dict[str, str] = {}

    def fake_query(system, user, model, **kw):
        captured["user"] = user
        return GROUPS_REPLY

    monkeypatch.setattr(rollup, "query", fake_query)
    build_groups(tmp_path)

    assert "PRIOR GROUPS.md:" not in captured["user"]
    assert "no prior GROUPS.md" in captured["user"]


def test_build_groups_feeds_the_prior_groups_back_into_the_prompt(
    tmp_path: Path, monkeypatch
):
    """Without prior state the grouping is re-imagined every run and churns."""
    import sagent.rollup as rollup

    _seed_three_projects(tmp_path)
    monkeypatch.setattr(rollup, "query", lambda *a, **kw: GROUPS_REPLY)
    build_groups(tmp_path)

    captured: dict[str, str] = {}

    def fake_query(system, user, model, **kw):
        captured["user"] = user
        return GROUPS_REPLY

    monkeypatch.setattr(rollup, "query", fake_query)
    build_groups(tmp_path)

    assert "PRIOR GROUPS.md:" in captured["user"]
    prior_block = captured["user"].split("PROJECT LIST:")[0]
    assert "## hms" in prior_block
    assert "- `hms-atlas` — the API" in prior_block
    assert "## Ungrouped" in prior_block


def test_build_groups_prior_block_carries_no_front_matter(
    tmp_path: Path, monkeypatch
):
    import sagent.rollup as rollup

    _seed_three_projects(tmp_path)
    monkeypatch.setattr(rollup, "query", lambda *a, **kw: GROUPS_REPLY)
    build_groups(tmp_path)

    captured: dict[str, str] = {}

    def fake_query(system, user, model, **kw):
        captured["user"] = user
        return GROUPS_REPLY

    monkeypatch.setattr(rollup, "query", fake_query)
    build_groups(tmp_path)

    prior_block = captured["user"].split("PROJECT LIST:")[0]
    assert "type: \"groups\"" not in prior_block
    assert "advisory: true" not in prior_block


def test_build_groups_needs_at_least_two_projects(tmp_path: Path, monkeypatch):
    import sagent.rollup as rollup

    _seed_digest(tmp_path, "hms-atlas", description="Only one.")
    calls: list[str] = []
    monkeypatch.setattr(
        rollup, "query", lambda *a, **kw: calls.append("called") or GROUPS_REPLY
    )

    assert build_groups(tmp_path) is None
    assert calls == []
    assert not (tmp_path / GROUPS_FILENAME).exists()


def test_build_groups_ignores_scratchpads(tmp_path: Path, monkeypatch):
    import sagent.rollup as rollup

    _seed_digest(tmp_path, "hms-atlas", description="The atlas API.")
    _seed_digest(tmp_path, "hms-rag", description="Retrieval for atlas.")
    _seed_digest(tmp_path, "home-user", kind="scratchpad")
    captured: dict[str, str] = {}

    def fake_query(system, user, model, **kw):
        captured["user"] = user
        return GROUPS_REPLY

    monkeypatch.setattr(rollup, "query", fake_query)
    out = build_groups(tmp_path)

    assert "`home-user`" not in captured["user"]
    assert "project_count: 2" in out.read_text()


def test_build_groups_empty_reply_keeps_the_prior_file(tmp_path: Path, monkeypatch):
    import sagent.rollup as rollup

    _seed_three_projects(tmp_path)
    monkeypatch.setattr(rollup, "query", lambda *a, **kw: GROUPS_REPLY)
    build_groups(tmp_path)
    before = (tmp_path / GROUPS_FILENAME).read_text()

    monkeypatch.setattr(rollup, "query", lambda *a, **kw: "   \n\n")
    assert build_groups(tmp_path) is None
    assert (tmp_path / GROUPS_FILENAME).read_text() == before


def test_update_index_skips_groups_by_default(tmp_path: Path, monkeypatch):
    import sagent.rollup as rollup

    _seed_three_projects(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(
        rollup, "query", lambda *a, **kw: calls.append("called") or GROUPS_REPLY
    )

    update_index(tmp_path)
    assert calls == []
    assert not (tmp_path / GROUPS_FILENAME).exists()


def test_update_index_refreshes_groups_when_a_model_is_given(
    tmp_path: Path, monkeypatch
):
    import sagent.rollup as rollup

    _seed_three_projects(tmp_path)
    monkeypatch.setattr(rollup, "query", lambda *a, **kw: GROUPS_REPLY)

    out = update_index(tmp_path, groups_model="claude-haiku-4-5")
    assert out is not None
    assert (tmp_path / GROUPS_FILENAME).exists()
    assert GROUPS_NOTICE in (tmp_path / GROUPS_FILENAME).read_text()


def test_update_index_survives_a_groups_failure(tmp_path: Path, monkeypatch):
    """GROUPS.md is advisory, so its failure must never cost the index."""
    import sagent.rollup as rollup

    _seed_three_projects(tmp_path)

    def boom(*a, **kw):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(rollup, "query", boom)

    out = update_index(tmp_path, groups_model="claude-haiku-4-5")
    assert out is not None
    assert "## Projects" in out.read_text()
    assert not (tmp_path / GROUPS_FILENAME).exists()


def test_groups_file_is_not_listed_as_a_project_by_the_index(
    tmp_path: Path, monkeypatch
):
    import sagent.rollup as rollup

    _seed_three_projects(tmp_path)
    monkeypatch.setattr(rollup, "query", lambda *a, **kw: GROUPS_REPLY)
    build_groups(tmp_path)

    text = update_index(tmp_path).read_text()
    assert "GROUPS" not in text


# ---------------------------------------------------------------------------
# Session block trimming (F5)
#
# The verbatim command appendix runs to its own 32_000-char budget while the
# roll-up gives one session 8_000 chars. Cutting on `\n## Timeline` alone left
# the commands in, so the shell filled the budget and the digest — the only
# part the roll-up can use — was truncated away. These tests are strict: not
# one byte of the appendix may reach the prompt.
# ---------------------------------------------------------------------------

APPENDIX_MARKER = "APPENDIX-ONLY-TOKEN"

F5_PROSE = (
    "---\n"
    'type: "session"\n'
    'harness: "claude-code"\n'
    'short_id: "a1b2c3d4"\n'
    "---\n"
    "# Session a1b2c3d4 — 2026-07-28\n"
    "\n"
    "_started 09:15 · 512 events · 3 prompts · 460 tool calls_\n"
    "\n"
    "## Summary\n"
    "\n"
    "Moved the atlas deploy target off proxmox and onto Azure Container Apps.\n"
    "\n"
    "## Understanding\n"
    "\n"
    "The session started from a failing rebuild on the proxmox host.\n"
    "\n"
    "## Decisions\n"
    "- **deploy to Azure Container Apps** (locked in 2026-07-28)\n"
    "\n"
    "## Open threads\n"
    "- audit the token budget (raised 2026-07-30)\n"
    "\n"
)

# The last line of the digest. Under the old cut it fell off the end of the
# 8_000-char budget, because the commands came first.
F5_LAST_DIGEST_LINE = "- audit the token budget (raised 2026-07-30)"


def _f5_commands(count: int = 400) -> list[str]:
    """Shell commands long enough that the appendix alone blows the budget."""
    return [
        f"nix build .#nixosConfigurations.nightman.config.system.build.toplevel "
        f"--print-build-logs --option substituters 'https://cache.nixos.org' "
        f"--marker {APPENDIX_MARKER}-{i:03d} 2>&1 | tail -40"
        for i in range(count)
    ]


def _f5_session_md(heading: str = "## Commands (verbatim)") -> tuple[str, list[str]]:
    """A session digest whose appendix dwarfs its prose. Returns (md, commands)."""
    commands = _f5_commands()
    appendix = [heading, "", f"### Shell commands ({len(commands)} of 460 shown)", ""]
    for cmd in commands:
        appendix.extend(["```", cmd, "```", ""])
    return F5_PROSE + "\n".join(appendix), commands


def test_f5_fixture_is_big_enough_to_matter():
    """Guard the fixture: a small appendix would prove nothing."""
    from sagent.rollup import _build_session_block

    md, _ = _f5_session_md()
    assert len(md) > 8_000
    assert len(F5_PROSE) < 8_000
    # Every byte over the budget in the raw document is appendix.
    assert len(md) - len(F5_PROSE) > 8_000
    assert len(_build_session_block(md)) < len(md)


def test_build_session_block_drops_the_verbatim_command_appendix():
    from sagent.rollup import _build_session_block

    md, commands = _f5_session_md()
    block = _build_session_block(md)

    assert "## Commands (verbatim)" not in block
    assert APPENDIX_MARKER not in block
    assert "```" not in block
    for cmd in commands:
        assert cmd not in block


def test_build_session_block_keeps_the_whole_digest_when_commands_are_huge():
    """The digest is what the roll-up reads, so none of it may be truncated."""
    from sagent.rollup import _build_session_block

    md, _ = _f5_session_md()
    block = _build_session_block(md)

    assert block == F5_PROSE.rstrip() + "\n"
    assert "## Summary" in block
    assert "Moved the atlas deploy target off proxmox" in block
    assert "## Understanding" in block
    assert F5_LAST_DIGEST_LINE in block
    # Nothing was cut for length, so no ellipsis marker.
    assert not block.endswith("…")
    assert len(block) < 8_000


def test_build_session_block_keeps_the_model_written_h2s_in_the_understanding():
    """Deny-list, not allow-list: the digest writes its own `## Decisions`."""
    from sagent.rollup import _build_session_block

    md, _ = _f5_session_md()
    block = _build_session_block(md)

    assert "## Decisions" in block
    assert "- **deploy to Azure Container Apps** (locked in 2026-07-28)" in block
    assert "## Open threads" in block


@pytest.mark.parametrize(
    "heading",
    ["## Commands (verbatim)", "## Commands", "## Timeline", "## Turn-by-turn"],
)
def test_build_session_block_cuts_every_appendix_heading(heading: str):
    """A rename of `Commands (verbatim)` must not re-open the hole."""
    from sagent.rollup import _build_session_block

    md, commands = _f5_session_md(heading=heading)
    block = _build_session_block(md)

    assert heading not in block
    assert APPENDIX_MARKER not in block
    assert commands[0] not in block
    assert block == F5_PROSE.rstrip() + "\n"


def test_build_session_block_still_caps_a_very_long_digest():
    """The budget still applies to prose — the appendix cut is not a bypass."""
    from sagent.rollup import _build_session_block

    md = F5_PROSE + ("Long prose about the deploy. " * 1_000)
    block = _build_session_block(md, max_chars=2_000)
    assert len(block) == 2_000
    assert block.endswith("…")


# ---------------------------------------------------------------------------
# Cross-module seam: the heading rollup cuts on is the heading session_doc
# emits. Both sides are covered on their own, and both would stay green if
# `render_verbatim_block` renamed or re-levelled its section — so this drives
# the real composer instead of a fixture string.
# ---------------------------------------------------------------------------


def _seam_session():
    from sagent.parser import Event, Session

    def ev(uid: str, **kw):
        return Event(uuid=uid, parent_uuid=None, **kw)

    return Session(
        session_id="seam0000",
        path=Path("/tmp/seam.jsonl"),
        cwd="/srv/seam",
        events=[
            ev("1", kind="user_prompt", text="ship it", timestamp="2026-01-01T00:00:00Z"),
            ev(
                "2",
                kind="tool_use",
                tool_name="Bash",
                tool_input={"command": "ls -la /srv/seam"},
                timestamp="2026-01-01T00:00:01Z",
            ),
            ev(
                "3",
                kind="tool_use",
                tool_name="Write",
                tool_input={"file_path": "/srv/seam/x.py"},
                timestamp="2026-01-01T00:00:02Z",
            ),
        ],
    )


def test_the_rollup_cut_matches_the_heading_session_doc_actually_emits():
    """D6's verbatim block must never reach the roll-up prompt."""
    from sagent.rollup import _APPENDIX_HEADING_RE, _build_session_block
    from sagent.session_doc import compose_session_md

    md = compose_session_md(
        _seam_session(),
        summary_md="Shipped the seam.",
        understanding_md="It holds.",
        project="seam",
    )
    assert "## Commands (verbatim)" in md, "composer no longer emits the block"
    assert _APPENDIX_HEADING_RE.search(md), "the cut no longer finds the block"

    block = _build_session_block(md)

    assert "```" not in block
    assert "ls -la /srv/seam" not in block
    assert "Commands" not in block
    assert "## Summary" in block and "## Understanding" in block


def test_every_h2_below_the_verbatim_heading_is_cut_from_the_roll_up_prompt():
    """The cut is positional, so a later H2 must not survive it."""
    from sagent.rollup import _APPENDIX_HEADING_RE, _build_session_block
    from sagent.session_doc import compose_session_md

    md = compose_session_md(
        _seam_session(),
        summary_md="Shipped the seam.",
        understanding_md="It holds.",
        project="seam",
    )
    cut = _APPENDIX_HEADING_RE.search(md).start()
    block = _build_session_block(md)

    for line in md[cut:].splitlines():
        if line.startswith("#"):
            assert line not in block, f"{line!r} survived the appendix cut"


def test_roll_up_prompt_carries_the_digest_and_no_command_text(
    tmp_path: Path, monkeypatch
):
    """End to end: the model never sees the verbatim block."""
    import sagent.rollup as rollup

    project_dir, session_path = _rollup_project(tmp_path, prior=PRIOR_PROJECT_MD)
    md, commands = _f5_session_md()
    session_path.write_text(md)

    captured: dict[str, str] = {}

    def fake_query(system, user, model, **kw):
        captured["user"] = user
        return VALID_ROLLUP_OUTPUT

    monkeypatch.setattr(rollup, "query", fake_query)
    monkeypatch.setattr(rollup, "read_project_context", lambda p: "")
    monkeypatch.setattr(rollup, "git_remote_url", lambda p: None)

    rollup.roll_up_project(project_dir, new_session_path=session_path)

    assert "Moved the atlas deploy target off proxmox" in captured["user"]
    assert F5_LAST_DIGEST_LINE in captured["user"]
    assert APPENDIX_MARKER not in captured["user"]
    for cmd in commands:
        assert cmd not in captured["user"]


def test_full_rebuild_prompt_carries_no_command_text(tmp_path: Path, monkeypatch):
    """The rebuild path packs many sessions, so one appendix starves the rest."""
    import sagent.rollup as rollup

    project_dir, session_path = _rollup_project(tmp_path, prior=PRIOR_PROJECT_MD)
    md, commands = _f5_session_md()
    session_path.write_text(md)

    captured: dict[str, str] = {}

    def fake_query(system, user, model, **kw):
        captured["user"] = user
        return VALID_ROLLUP_OUTPUT

    monkeypatch.setattr(rollup, "query", fake_query)
    monkeypatch.setattr(rollup, "read_project_context", lambda p: "")
    monkeypatch.setattr(rollup, "git_remote_url", lambda p: None)

    rollup.roll_up_project(
        project_dir, new_session_path=session_path, force_full=True
    )

    assert "Moved the atlas deploy target off proxmox" in captured["user"]
    assert APPENDIX_MARKER not in captured["user"]
    for cmd in commands:
        assert cmd not in captured["user"]


# ---------------------------------------------------------------------------
# GROUPS.md refresh gate (F6)
#
# The index runs after every roll-up, so an ungated refresh would buy one
# grouping call per session. The gate must not turn into "never refreshed".
# ---------------------------------------------------------------------------


def _age_file(path: Path, hours: float) -> None:
    """Backdate mtime so the age gate sees the file as `hours` old."""
    import os
    import time

    when = time.time() - hours * 3600.0
    os.utime(path, (when, when))


def test_update_index_builds_groups_on_the_first_pass(tmp_path: Path, monkeypatch):
    """A missing GROUPS.md is always built, so a fresh host still gets one."""
    import sagent.rollup as rollup

    _seed_three_projects(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(
        rollup, "query", lambda *a, **kw: calls.append("called") or GROUPS_REPLY
    )

    update_index(tmp_path, groups_model="claude-haiku-4-5")

    assert len(calls) == 1
    assert (tmp_path / GROUPS_FILENAME).exists()


def test_update_index_skips_the_groups_refresh_while_it_is_fresh(
    tmp_path: Path, monkeypatch
):
    """A batch of twenty sessions must not buy twenty groupings."""
    import sagent.rollup as rollup

    _seed_three_projects(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(
        rollup, "query", lambda *a, **kw: calls.append("called") or GROUPS_REPLY
    )

    update_index(tmp_path, groups_model="claude-haiku-4-5")
    before = (tmp_path / GROUPS_FILENAME).read_text()
    for _ in range(5):
        update_index(tmp_path, groups_model="claude-haiku-4-5")

    assert len(calls) == 1
    assert (tmp_path / GROUPS_FILENAME).read_text() == before


def test_update_index_refreshes_groups_once_it_ages_out(tmp_path: Path, monkeypatch):
    import sagent.rollup as rollup

    _seed_three_projects(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(
        rollup, "query", lambda *a, **kw: calls.append("called") or GROUPS_REPLY
    )

    update_index(tmp_path, groups_model="claude-haiku-4-5")
    _age_file(tmp_path / GROUPS_FILENAME, hours=25)
    update_index(tmp_path, groups_model="claude-haiku-4-5")

    assert len(calls) == 2


def test_update_index_honours_a_custom_max_age(tmp_path: Path, monkeypatch):
    import sagent.rollup as rollup

    _seed_three_projects(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(
        rollup, "query", lambda *a, **kw: calls.append("called") or GROUPS_REPLY
    )

    update_index(tmp_path, groups_model="claude-haiku-4-5")
    _age_file(tmp_path / GROUPS_FILENAME, hours=2)

    update_index(tmp_path, groups_model="claude-haiku-4-5", groups_max_age_hours=6.0)
    assert len(calls) == 1

    update_index(tmp_path, groups_model="claude-haiku-4-5", groups_max_age_hours=1.0)
    assert len(calls) == 2


def test_update_index_forces_the_refresh_at_zero_age(tmp_path: Path, monkeypatch):
    """What an explicit `--groups` asks for: rebuild now, however fresh."""
    import sagent.rollup as rollup

    _seed_three_projects(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(
        rollup, "query", lambda *a, **kw: calls.append("called") or GROUPS_REPLY
    )

    update_index(tmp_path, groups_model="claude-haiku-4-5", groups_max_age_hours=0.0)
    update_index(tmp_path, groups_model="claude-haiku-4-5", groups_max_age_hours=0.0)

    assert len(calls) == 2


def test_update_index_makes_no_llm_call_without_a_model(tmp_path: Path, monkeypatch):
    """`--no-llm` passes groups_model=None, so the pass stays offline."""
    import sagent.rollup as rollup

    _seed_three_projects(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(
        rollup, "query", lambda *a, **kw: calls.append("called") or GROUPS_REPLY
    )

    # Missing file and a forced age: still no call, because there is no model.
    out = update_index(tmp_path, groups_model=None, groups_max_age_hours=0.0)

    assert calls == []
    assert not (tmp_path / GROUPS_FILENAME).exists()
    assert out is not None and out.exists()


# ---------------------------------------------------------------------------
# Harness attribution on the incremental path (F7)
# ---------------------------------------------------------------------------


def _seed_session(sessions_dir: Path, name: str, *, harness: str | None) -> Path:
    harness_line = f'harness: "{harness}"\n' if harness else ""
    p = sessions_dir / name
    p.write_text(
        "---\n"
        'type: "session"\n'
        f"{harness_line}"
        "---\n"
        f"# Session {name[:-3]}\n\n## Summary\n\nDid a thing.\n"
    )
    return p


def test_incremental_roll_up_reports_every_harness_already_on_disk(
    tmp_path: Path, monkeypatch
):
    """F7: a project.md predating opencode ingestion carries no harness list.

    Trusting the new session plus the prior front matter hid the opencode
    digests already in sessions/ until the next full rebuild.
    """
    import sagent.rollup as rollup
    from sagent.frontmatter import split_front_matter

    project_dir = tmp_path / "hms-atlas"
    sessions = project_dir / "sessions"
    sessions.mkdir(parents=True)
    _seed_session(sessions, "2026-07-26-11111111.md", harness="opencode")
    _seed_session(sessions, "2026-07-27-22222222.md", harness="opencode")
    new_session = _seed_session(sessions, "2026-07-28-a1b2c3d4.md", harness="claude-code")

    assert "harnesses" not in PRIOR_PROJECT_MD
    (project_dir / "project.md").write_text(PRIOR_PROJECT_MD)
    _stub_rollup(monkeypatch, VALID_ROLLUP_OUTPUT)

    out = rollup.roll_up_project(project_dir, new_session_path=new_session)

    fm, _ = split_front_matter(out.read_text())
    assert fm["harnesses"] == ["claude-code", "opencode"]


def test_incremental_roll_up_treats_a_missing_harness_field_as_claude_code(
    tmp_path: Path, monkeypatch
):
    import sagent.rollup as rollup
    from sagent.frontmatter import split_front_matter

    project_dir = tmp_path / "hms-atlas"
    sessions = project_dir / "sessions"
    sessions.mkdir(parents=True)
    _seed_session(sessions, "2026-07-26-11111111.md", harness=None)
    new_session = _seed_session(sessions, "2026-07-28-a1b2c3d4.md", harness="opencode")

    (project_dir / "project.md").write_text(PRIOR_PROJECT_MD)
    _stub_rollup(monkeypatch, VALID_ROLLUP_OUTPUT)

    out = rollup.roll_up_project(project_dir, new_session_path=new_session)

    fm, _ = split_front_matter(out.read_text())
    assert fm["harnesses"] == ["claude-code", "opencode"]


def test_incremental_and_full_rebuild_agree_on_the_harness_list(
    tmp_path: Path, monkeypatch
):
    """Both branches read the same source, so they must report the same list."""
    import sagent.rollup as rollup
    from sagent.frontmatter import split_front_matter

    def _build(dirname: str) -> tuple[Path, Path]:
        project_dir = tmp_path / dirname
        sessions = project_dir / "sessions"
        sessions.mkdir(parents=True)
        _seed_session(sessions, "2026-07-26-11111111.md", harness="opencode")
        new_session = _seed_session(
            sessions, "2026-07-28-a1b2c3d4.md", harness="claude-code"
        )
        (project_dir / "project.md").write_text(PRIOR_PROJECT_MD)
        return project_dir, new_session

    _stub_rollup(monkeypatch, VALID_ROLLUP_OUTPUT)

    inc_dir, inc_session = _build("inc")
    out_inc = rollup.roll_up_project(inc_dir, new_session_path=inc_session)

    full_dir, full_session = _build("full")
    out_full = rollup.roll_up_project(
        full_dir, new_session_path=full_session, force_full=True
    )

    fm_inc, _ = split_front_matter(out_inc.read_text())
    fm_full, _ = split_front_matter(out_full.read_text())
    assert fm_inc["harnesses"] == fm_full["harnesses"] == ["claude-code", "opencode"]
