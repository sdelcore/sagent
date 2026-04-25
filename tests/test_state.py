from __future__ import annotations

import json
from pathlib import Path

from sagent.state import CURRENT_VERSION, SessionRecord, StateStore, default_state_path


def test_default_state_path_uses_sagent_state(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("SAGENT_STATE", str(tmp_path / "x.json"))
    assert default_state_path() == tmp_path / "x.json"


def test_default_state_path_uses_xdg(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("SAGENT_STATE", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert default_state_path() == tmp_path / "state" / "sagent" / "state.json"


def test_default_state_path_falls_back(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("SAGENT_STATE", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert default_state_path() == tmp_path / ".local" / "state" / "sagent" / "state.json"


def test_load_missing_file_is_empty(tmp_path: Path):
    s = StateStore(tmp_path / "missing.json")
    assert s.sessions == {}


def test_save_and_load_roundtrip(tmp_path: Path):
    p = tmp_path / "s.json"
    s1 = StateStore(p)
    s1.mark_digested("/a/b.jsonl", size=1234, event_index=10)
    s1.mark_digested("/c/d.jsonl", size=5678, event_index=42)
    s1.save()

    s2 = StateStore(p)
    assert set(s2.sessions.keys()) == {"/a/b.jsonl", "/c/d.jsonl"}
    assert s2.sessions["/a/b.jsonl"].last_digested_size == 1234
    assert s2.sessions["/a/b.jsonl"].last_event_index == 10
    assert s2.sessions["/a/b.jsonl"].digest_count == 1


def test_corrupt_file_is_tolerated(tmp_path: Path):
    p = tmp_path / "s.json"
    p.write_text("{ this is not json")
    s = StateStore(p)
    assert s.sessions == {}


def test_unknown_keys_are_ignored(tmp_path: Path):
    p = tmp_path / "s.json"
    p.write_text(
        json.dumps(
            {
                "version": 99,
                "sessions": {
                    "/x.jsonl": {
                        "last_digested_size": 100,
                        "last_event_index": 5,
                        "future_field": "ignored",
                    }
                },
            }
        )
    )
    s = StateStore(p)
    assert "/x.jsonl" in s.sessions
    assert s.sessions["/x.jsonl"].last_digested_size == 100


def test_should_skip_when_size_unchanged(tmp_path: Path):
    s = StateStore(tmp_path / "s.json")
    s.mark_digested("/a.jsonl", size=1000, event_index=5)
    assert s.should_skip("/a.jsonl", size=1000)
    assert s.should_skip("/a.jsonl", size=999)
    assert not s.should_skip("/a.jsonl", size=1001)


def test_should_skip_with_min_delta(tmp_path: Path):
    s = StateStore(tmp_path / "s.json")
    s.mark_digested("/a.jsonl", size=1000, event_index=5)
    assert s.should_skip("/a.jsonl", size=1500, min_delta=600)
    assert not s.should_skip("/a.jsonl", size=1700, min_delta=600)


def test_should_skip_unknown_session(tmp_path: Path):
    s = StateStore(tmp_path / "s.json")
    assert not s.should_skip("/never-seen.jsonl", size=1234)


def test_mark_digested_increments_count(tmp_path: Path):
    s = StateStore(tmp_path / "s.json")
    s.mark_digested("/a.jsonl", size=100, event_index=1)
    s.mark_digested("/a.jsonl", size=200, event_index=2)
    s.mark_digested("/a.jsonl", size=300, event_index=3)
    assert s.sessions["/a.jsonl"].digest_count == 3
    assert s.sessions["/a.jsonl"].last_digested_size == 300


def test_save_is_atomic(tmp_path: Path):
    """Crash mid-save shouldn't leave a partially-written file."""
    p = tmp_path / "s.json"
    s = StateStore(p)
    s.mark_digested("/a.jsonl", size=100, event_index=1)
    s.save()
    # Final file should be valid json
    data = json.loads(p.read_text())
    assert data["version"] == CURRENT_VERSION
    # No leftover .tmp files
    leftovers = list(tmp_path.glob(".state-*.tmp"))
    assert leftovers == []


def test_prune_missing(tmp_path: Path):
    s = StateStore(tmp_path / "s.json")
    s.mark_digested("/exists.jsonl", size=1, event_index=1)
    s.mark_digested("/gone.jsonl", size=1, event_index=1)
    removed = s.prune_missing({Path("/exists.jsonl")})
    assert removed == 1
    assert "/exists.jsonl" in s.sessions
    assert "/gone.jsonl" not in s.sessions
