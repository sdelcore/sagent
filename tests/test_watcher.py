from __future__ import annotations

from pathlib import Path

from sagent.watcher import (
    CLAUDE_PROJECTS,
    SettleTracker,
    latest_session,
    project_dir_for_cwd,
    watch_all,
)


def test_project_dir_for_cwd_encodes_slashes():
    p = project_dir_for_cwd("/home/user/src/proj")
    assert p == CLAUDE_PROJECTS / "-home-user-src-proj"


def test_project_dir_for_cwd_pathlike():
    p = project_dir_for_cwd(Path("/a/b"))
    assert p.name == "-a-b"


def test_latest_session_missing_dir(tmp_path: Path):
    assert latest_session(tmp_path / "nope") is None


def test_latest_session_empty_dir(tmp_path: Path):
    assert latest_session(tmp_path) is None


def test_latest_session_picks_most_recent(tmp_path: Path):
    import time

    old = tmp_path / "old.jsonl"
    new = tmp_path / "new.jsonl"
    old.write_text("{}")
    time.sleep(0.02)
    new.write_text("{}")
    assert latest_session(tmp_path) == new


def test_watch_all_is_callable():
    assert callable(watch_all)


# ---------------------------------------------------------------------------
# SettleTracker — drives the three watch loops via injected `now`.
# ---------------------------------------------------------------------------


def test_tracker_does_not_fire_before_quiet_window():
    t = SettleTracker(quiet_seconds=300)
    p = Path("/x.jsonl")
    assert not t.tick(p, 100, now=0)
    # Same size, but only 10s elapsed.
    assert not t.tick(p, 100, now=10)


def test_tracker_fires_once_after_settle():
    t = SettleTracker(quiet_seconds=300)
    p = Path("/x.jsonl")
    t.tick(p, 100, now=0)
    # Crosses the threshold.
    assert t.tick(p, 100, now=301) is True
    t.mark_fired(p, 100)
    # Doesn't re-fire at the same size.
    assert t.tick(p, 100, now=400) is False


def test_tracker_growth_resets_change_clock():
    t = SettleTracker(quiet_seconds=300)
    p = Path("/x.jsonl")
    t.tick(p, 100, now=0)
    # File grew at t=200; the quiet clock should reset to 200.
    t.tick(p, 200, now=200)
    # 250s after the original observation but only 50s after growth — not yet.
    assert not t.tick(p, 200, now=250)
    # 350s after growth — now it fires.
    assert t.tick(p, 200, now=550) is True


def test_tracker_zero_size_never_fires():
    t = SettleTracker(quiet_seconds=300)
    p = Path("/empty.jsonl")
    t.tick(p, 0, now=0)
    assert not t.tick(p, 0, now=10_000)


def test_tracker_hydrate_suppresses_initial_fire():
    """Across a service restart, hydrate() seeds prior state so we don't
    re-digest a session whose size hasn't changed."""
    t = SettleTracker(quiet_seconds=300)
    p = Path("/x.jsonl")
    t.hydrate(p, 1000)
    # First tick observes the same size — no change clock starts; would not
    # cross the threshold even if we waited forever.
    assert not t.tick(p, 1000, now=10_000)


def test_tracker_reset_forgets_path():
    t = SettleTracker(quiet_seconds=300)
    p = Path("/x.jsonl")
    t.tick(p, 100, now=0)
    t.tick(p, 100, now=301)
    t.mark_fired(p, 100)
    t.reset(p)
    # After reset, fresh observation — needs another full quiet window.
    assert not t.tick(p, 100, now=350)
    assert t.tick(p, 100, now=700) is True
