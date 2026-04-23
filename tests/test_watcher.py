from __future__ import annotations

from pathlib import Path

from sagent.watcher import CLAUDE_PROJECTS, latest_session, project_dir_for_cwd, watch_all


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
