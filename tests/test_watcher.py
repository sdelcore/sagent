from __future__ import annotations

import getpass
import sqlite3
from pathlib import Path

from sagent import opencode
from sagent.opencode import SessionRow
from sagent.rate import SagentRateLimitError
from sagent.state import NullLedger
from sagent.watcher import (
    CLAUDE_PROJECTS,
    SettleGate,
    SettleTracker,
    _opencode_pass,
    _process,
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


# ---------------------------------------------------------------------------
# SettleGate — the shared "is this ready to digest?" decision
# ---------------------------------------------------------------------------

KEY = "opencode://ses_a"


def _gate(**kw) -> SettleGate:
    return SettleGate(
        tracker=SettleTracker(quiet_seconds=0.0),
        ledger=NullLedger(),
        **kw,
    )


def test_gate_refuses_a_session_below_min_bytes():
    gate = _gate(min_bytes=5_000)
    assert gate.due(KEY, 4_999) is False


def test_gate_fires_once_a_uri_keyed_session_stops_growing():
    gate = _gate()
    assert gate.due(KEY, 1_000) is False  # first sighting records the size
    assert gate.due(KEY, 1_000) is True  # unchanged past the quiet window
    gate.mark_fired(KEY, 1_000)
    assert gate.due(KEY, 1_000) is False


def test_gate_defers_while_a_uri_keyed_session_still_grows():
    gate = _gate()
    gate.due(KEY, 1_000)
    assert gate.due(KEY, 2_000) is False
    assert gate.due(KEY, 2_000) is True


def test_gate_skips_what_the_ledger_already_digested():
    ledger = NullLedger()
    ledger.mark_digested(KEY, size=1_000, event_index=4)
    gate = SettleGate(tracker=SettleTracker(quiet_seconds=0.0), ledger=ledger)
    # Settle the tracker first, so a False answer can only come from the
    # ledger. Asserting on a first sighting would pass either way — the
    # tracker always defers the first time it sees a size.
    gate.tracker.tick(KEY, 1_000)
    assert gate.tracker.tick(KEY, 1_000) is True
    assert gate.due(KEY, 1_000) is False


def test_gate_hydration_keeps_a_uri_key_a_string():
    ledger = NullLedger()
    ledger.mark_digested(KEY, size=1_000, event_index=4)
    gate = SettleGate(tracker=SettleTracker(quiet_seconds=0.0), ledger=ledger)
    assert gate.hydrate_from_ledger() == 1
    # Path() would collapse the double slash into "opencode:/ses_a" and the
    # hydrated entry would never match the key the sweep presents.
    assert KEY in gate.tracker._fired_for_size


# ---------------------------------------------------------------------------
# _opencode_pass — one sweep over the database
# ---------------------------------------------------------------------------


def _rows(*specs) -> list[SessionRow]:
    return [
        SessionRow(session_id=s, directory=d, project_id="global", time_updated=t)
        for s, d, t in specs
    ]


def _patch_db(monkeypatch, rows, sizes):
    monkeypatch.setattr(opencode, "list_sessions", lambda db: rows)
    monkeypatch.setattr(
        opencode, "session_bytes", lambda db, sid: sizes.get(sid, 0)
    )


def test_opencode_pass_digests_a_settled_session(monkeypatch, tmp_path: Path):
    rows = _rows(("ses_a", "/home/u/src/app", 10))
    _patch_db(monkeypatch, rows, {"ses_a": 9_000})
    gate = _gate()
    fired: list = []
    for _ in range(2):
        _opencode_pass(
            gate=gate, db=tmp_path / "o.db", on_change=fired.append, cooldown=0.0
        )
    assert [t.session_id for t in fired] == ["ses_a"]
    target = fired[0]
    assert target.ledger_key == "opencode://ses_a"
    assert target.size == 9_000
    assert target.row.directory == "/home/u/src/app"


def test_opencode_pass_never_fires_twice_for_one_size(monkeypatch, tmp_path: Path):
    rows = _rows(("ses_a", "/home/u/src/app", 10))
    _patch_db(monkeypatch, rows, {"ses_a": 9_000})
    gate = _gate()
    fired: list = []
    for _ in range(5):
        _opencode_pass(
            gate=gate, db=tmp_path / "o.db", on_change=fired.append, cooldown=0.0
        )
    assert len(fired) == 1


def test_opencode_pass_puts_real_projects_before_scratchpads(
    monkeypatch, tmp_path: Path
):
    user = getpass.getuser()
    rows = _rows(
        (f"ses_scratch", f"/home/{user}", 30),
        ("ses_proj", "/home/u/src/app", 10),
    )
    _patch_db(monkeypatch, rows, {"ses_scratch": 9_000, "ses_proj": 9_000})
    gate = _gate()
    fired: list = []
    for _ in range(2):
        _opencode_pass(
            gate=gate, db=tmp_path / "o.db", on_change=fired.append, cooldown=0.0
        )
    assert [t.session_id for t in fired] == ["ses_proj", "ses_scratch"]


def test_opencode_pass_survives_an_unreadable_database(monkeypatch, tmp_path: Path):
    def boom(db):
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr(opencode, "list_sessions", boom)
    fired: list = []
    assert (
        _opencode_pass(
            gate=_gate(), db=tmp_path / "o.db", on_change=fired.append, cooldown=0.0
        )
        is False
    )
    assert fired == []


def test_opencode_pass_skips_a_session_it_cannot_size(monkeypatch, tmp_path: Path):
    rows = _rows(("ses_bad", "/home/u/src/app", 10), ("ses_ok", "/home/u/src/b", 20))

    def sizes(db, sid):
        if sid == "ses_bad":
            raise sqlite3.OperationalError("gone")
        return 9_000

    monkeypatch.setattr(opencode, "list_sessions", lambda db: rows)
    monkeypatch.setattr(opencode, "session_bytes", sizes)
    gate = _gate()
    fired: list = []
    for _ in range(2):
        _opencode_pass(
            gate=gate, db=tmp_path / "o.db", on_change=fired.append, cooldown=0.0
        )
    assert [t.session_id for t in fired] == ["ses_ok"]


def test_opencode_pass_abandons_the_sweep_on_a_rate_limit(
    monkeypatch, tmp_path: Path
):
    rows = _rows(("ses_a", "/home/u/src/a", 10), ("ses_b", "/home/u/src/b", 20))
    _patch_db(monkeypatch, rows, {"ses_a": 9_000, "ses_b": 9_000})
    gate = _gate()
    seen: list = []

    def on_change(target):
        seen.append(target.session_id)
        raise SagentRateLimitError("slow down")

    _opencode_pass(gate=gate, db=tmp_path / "o.db", on_change=on_change, cooldown=0.0)
    assert (
        _opencode_pass(
            gate=gate, db=tmp_path / "o.db", on_change=on_change, cooldown=0.0
        )
        is True
    )
    # Only the first session was attempted, and it was never marked fired, so
    # it is retried once the window reopens.
    assert seen == ["ses_a"]


# ---------------------------------------------------------------------------
# A refused roll-up must stay retryable (F8)
#
# The ledger claim is deliberately left uncommitted so the session is picked
# up again. That only works if the in-memory tracker also leaves it unfired:
# a settled session never changes size again, so marking it fired buries it
# for the life of the process.
# ---------------------------------------------------------------------------


def _due_gate(key: str = KEY) -> SettleGate:
    gate = _gate()
    gate.due(key, 1_000)  # first sighting records the size
    return gate


def test_a_refused_rollup_leaves_the_session_due_on_the_next_pass():
    gate = _due_gate()
    assert _process(
        gate, KEY, 1_000, label=KEY, run=lambda: False, cooldown=0.0
    ) is False
    # Still due: nothing was marked fired, so the next sweep retries it.
    assert gate.due(KEY, 1_000) is True


def test_a_successful_digest_is_fired_and_not_repeated():
    gate = _due_gate()
    _process(gate, KEY, 1_000, label=KEY, run=lambda: True, cooldown=0.0)
    assert gate.due(KEY, 1_000) is False


def test_a_callback_returning_none_still_counts_as_settled():
    """The Claude Code loops predate the bool contract and return None."""
    gate = _due_gate()
    _process(gate, KEY, 1_000, label=KEY, run=lambda: None, cooldown=0.0)
    assert gate.due(KEY, 1_000) is False


def test_the_retry_survives_repeated_refusals():
    gate = _due_gate()
    for _ in range(3):
        assert gate.due(KEY, 1_000) is True
        _process(gate, KEY, 1_000, label=KEY, run=lambda: False, cooldown=0.0)
    assert gate.due(KEY, 1_000) is True
