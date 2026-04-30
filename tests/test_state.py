from __future__ import annotations

import json
from pathlib import Path

from sagent.state import (
    CURRENT_VERSION,
    DigestLedger,
    NullLedger,
    SessionRecord,
    default_state_path,
)


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
    s = DigestLedger(tmp_path / "missing.json")
    assert s.sessions == {}


def test_save_and_load_roundtrip(tmp_path: Path):
    p = tmp_path / "s.json"
    s1 = DigestLedger(p)
    s1.mark_digested("/a/b.jsonl", size=1234, event_index=10)
    s1.mark_digested("/c/d.jsonl", size=5678, event_index=42)
    s1.save()

    s2 = DigestLedger(p)
    assert set(s2.sessions.keys()) == {"/a/b.jsonl", "/c/d.jsonl"}
    assert s2.sessions["/a/b.jsonl"].last_digested_size == 1234
    assert s2.sessions["/a/b.jsonl"].last_event_index == 10
    assert s2.sessions["/a/b.jsonl"].digest_count == 1


def test_corrupt_file_is_tolerated(tmp_path: Path):
    p = tmp_path / "s.json"
    p.write_text("{ this is not json")
    s = DigestLedger(p)
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
    s = DigestLedger(p)
    assert "/x.jsonl" in s.sessions
    assert s.sessions["/x.jsonl"].last_digested_size == 100


def test_should_skip_when_size_unchanged(tmp_path: Path):
    s = DigestLedger(tmp_path / "s.json")
    s.mark_digested("/a.jsonl", size=1000, event_index=5)
    assert s.should_skip("/a.jsonl", size=1000)
    assert s.should_skip("/a.jsonl", size=999)
    assert not s.should_skip("/a.jsonl", size=1001)


def test_should_skip_with_min_delta(tmp_path: Path):
    s = DigestLedger(tmp_path / "s.json")
    s.mark_digested("/a.jsonl", size=1000, event_index=5)
    assert s.should_skip("/a.jsonl", size=1500, min_delta=600)
    assert not s.should_skip("/a.jsonl", size=1700, min_delta=600)


def test_should_skip_unknown_session(tmp_path: Path):
    s = DigestLedger(tmp_path / "s.json")
    assert not s.should_skip("/never-seen.jsonl", size=1234)


def test_mark_digested_increments_count(tmp_path: Path):
    s = DigestLedger(tmp_path / "s.json")
    s.mark_digested("/a.jsonl", size=100, event_index=1)
    s.mark_digested("/a.jsonl", size=200, event_index=2)
    s.mark_digested("/a.jsonl", size=300, event_index=3)
    assert s.sessions["/a.jsonl"].digest_count == 3
    assert s.sessions["/a.jsonl"].last_digested_size == 300


def test_save_is_atomic(tmp_path: Path):
    """Crash mid-save shouldn't leave a partially-written file."""
    p = tmp_path / "s.json"
    s = DigestLedger(p)
    s.mark_digested("/a.jsonl", size=100, event_index=1)
    s.save()
    # Final file should be valid json
    data = json.loads(p.read_text())
    assert data["version"] == CURRENT_VERSION
    # No leftover .tmp files
    leftovers = list(tmp_path.glob(".state-*.tmp"))
    assert leftovers == []


def test_prune_missing(tmp_path: Path):
    s = DigestLedger(tmp_path / "s.json")
    s.mark_digested("/exists.jsonl", size=1, event_index=1)
    s.mark_digested("/gone.jsonl", size=1, event_index=1)
    removed = s.prune_missing({Path("/exists.jsonl")})
    assert removed == 1
    assert "/exists.jsonl" in s.sessions
    assert "/gone.jsonl" not in s.sessions


# ---------------------------------------------------------------------------
# Claim/commit API
# ---------------------------------------------------------------------------


def test_claim_returns_none_when_already_digested(tmp_path: Path):
    led = DigestLedger(tmp_path / "s.json")
    led.mark_digested("/a.jsonl", size=1000, event_index=5)
    assert led.claim("/a.jsonl", size=1000) is None
    assert led.claim("/a.jsonl", size=999) is None


def test_claim_returns_claim_when_new(tmp_path: Path):
    led = DigestLedger(tmp_path / "s.json")
    claim = led.claim("/new.jsonl", size=1234)
    assert claim is not None
    assert claim.session_path == Path("/new.jsonl")
    assert claim.size == 1234
    assert claim.prior is None


def test_claim_carries_prior_record(tmp_path: Path):
    led = DigestLedger(tmp_path / "s.json")
    led.mark_digested("/a.jsonl", size=100, event_index=5)
    claim = led.claim("/a.jsonl", size=200)
    assert claim is not None
    assert claim.prior is not None
    assert claim.prior.last_event_index == 5
    assert claim.prior.digest_count == 1


def test_uncommitted_claim_leaves_state_untouched(tmp_path: Path):
    led = DigestLedger(tmp_path / "s.json")
    claim = led.claim("/a.jsonl", size=100)
    assert claim is not None
    # ... pretend the work crashed; never call commit() ...
    # State must show no record for /a.jsonl.
    assert led.get("/a.jsonl") is None
    # Re-claiming should still succeed (no skip).
    assert led.claim("/a.jsonl", size=100) is not None


def test_commit_persists_to_disk(tmp_path: Path):
    p = tmp_path / "s.json"
    led = DigestLedger(p)
    claim = led.claim("/a.jsonl", size=500)
    assert claim is not None
    claim.commit(event_index=42)
    # Reload and verify it stuck.
    reloaded = DigestLedger(p)
    rec = reloaded.get("/a.jsonl")
    assert rec is not None
    assert rec.last_digested_size == 500
    assert rec.last_event_index == 42


def test_force_returns_claim_even_when_skippable(tmp_path: Path):
    led = DigestLedger(tmp_path / "s.json")
    led.mark_digested("/a.jsonl", size=1000, event_index=5)
    assert led.claim("/a.jsonl", size=1000, force=True) is not None


def test_claim_rollup_carries_prior_count(tmp_path: Path):
    led = DigestLedger(tmp_path / "s.json")
    led.mark_rolled_up("proj", session_id="abc")
    led.mark_rolled_up("proj", session_id="def")
    rollup = led.claim_rollup("proj")
    assert rollup.prior_count == 2


def test_rollup_commit_increments_count(tmp_path: Path):
    p = tmp_path / "s.json"
    led = DigestLedger(p)
    led.claim_rollup("proj").commit(session_id="aaa")
    led.claim_rollup("proj").commit(session_id="bbb")
    assert led.get_project("proj").rollup_count == 2  # type: ignore[union-attr]
    assert led.get_project("proj").last_rolled_up_session_id == "bbb"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# NullLedger
# ---------------------------------------------------------------------------


def test_null_ledger_save_writes_nothing(tmp_path: Path):
    """NullLedger.save() must be a no-op — no file appears anywhere."""
    led = NullLedger()
    claim = led.claim("/a.jsonl", size=100)
    assert claim is not None
    claim.commit(event_index=10)
    # No file in tmp_path or anywhere — explicit check that save did nothing.
    assert list(tmp_path.iterdir()) == []


def test_null_ledger_remembers_in_memory(tmp_path: Path):
    """In-memory state still works (so the watcher's hydration loop stays sane)."""
    led = NullLedger()
    led.claim("/a.jsonl", size=100).commit(event_index=10)  # type: ignore[union-attr]
    # Same process: skip works.
    assert led.claim("/a.jsonl", size=100) is None


def test_null_ledger_starts_empty():
    """No `.sessions` from disk — hydration loop is a no-op."""
    led = NullLedger()
    assert led.sessions == {}
    assert led.projects == {}
