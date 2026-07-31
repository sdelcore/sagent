"""Polling loops that decide *when* a session is ready to digest.

Two harnesses are watched. Claude Code appends to a JSONL file per
session, so a directory scan plus `st_size` answers "did it grow?".
Opencode keeps every session in one SQLite database, so the same question
is answered by a query — never by the database file's mtime, which lags
real activity by minutes while an opencode server holds the connection.

Both loops ask the same four questions in the same order (big enough, not
already digested, stopped growing, fire once), so `SettleGate` owns those
and each loop only owns its enumeration.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import opencode
from .opencode import SessionRow
from .rate import SagentRateLimitError
from .state import DigestLedger, NullLedger, normalize_key

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"

DEFAULT_QUIET_SECONDS = 300.0  # 5 minutes; "summarize when a session goes idle"

# The opencode sweep costs a handful of SQLite queries, so it runs on its
# own slower clock than the filesystem scan. The settle window is minutes
# wide; polling the database every 30s cannot make a digest noticeably
# late.
DEFAULT_DB_POLL_SECONDS = 30.0


def project_dir_for_cwd(cwd: str | Path) -> Path:
    """Claude Code encodes the cwd path as directory name with / → -."""
    return CLAUDE_PROJECTS / str(cwd).replace("/", "-")


def latest_session(project_dir: Path) -> Path | None:
    if not project_dir.exists():
        return None
    sessions = sorted(
        project_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    return sessions[0] if sessions else None


# ---------------------------------------------------------------------------
# Idle-settle bookkeeping. The three watch loops below share this.
# ---------------------------------------------------------------------------


@dataclass
class SettleTracker:
    """Per-session "has this stopped growing for `quiet_seconds`?" tracker.

    The watch loops differ in *which* sessions to poll; the bookkeeping is
    the same — record sizes, note the moment a session's size last changed,
    and fire exactly once per (key, size) pair after the quiet window.

    A key is whatever the ledger keys on: a JSONL path for Claude Code, the
    string `opencode://<id>` for opencode. A size is any count that grows
    with the session, so a database byte total works as well as `st_size`.

    Use:
      tracker = SettleTracker(quiet_seconds=300)
      tracker.hydrate(prev_key, prev_size)  # suppress re-fire across restarts
      ...
      if tracker.tick(key, current_size):
          on_change(key)
          tracker.mark_fired(key, current_size)
    """

    quiet_seconds: float
    _last_size: dict[Path | str, int] = field(default_factory=dict)
    _last_change_at: dict[Path | str, float] = field(default_factory=dict)
    _fired_for_size: dict[Path | str, int] = field(default_factory=dict)

    def hydrate(self, path: Path | str, size: int) -> None:
        """Pre-mark `path` at `size` as already fired.

        Used on watcher startup so paths the ledger already knows about
        don't re-fire just because the polling loop is starting fresh.
        """
        self._fired_for_size[path] = size
        self._last_size[path] = size

    def tick(self, path: Path | str, size: int, *, now: float | None = None) -> bool:
        """Update bookkeeping for `path` at `size`. Return True if it just
        crossed the quiet threshold (caller should fire on_change).
        """
        if now is None:
            now = time.monotonic()
        if size <= 0:
            return False
        prev = self._last_size.get(path, -1)
        if size != prev:
            self._last_size[path] = size
            self._last_change_at[path] = now
            return False
        if self._fired_for_size.get(path, -1) == size:
            return False
        return now - self._last_change_at.get(path, now) >= self.quiet_seconds

    def mark_fired(self, path: Path | str, size: int) -> None:
        self._fired_for_size[path] = size

    def reset(self, path: Path | str) -> None:
        """Forget all state for `path`. Used by watch_project when the
        active session changes."""
        self._last_size.pop(path, None)
        self._last_change_at.pop(path, None)
        self._fired_for_size.pop(path, None)


@dataclass
class SettleGate:
    """"Is this session ready to digest?" — the part both loops share.

    A candidate passes when it is worth digesting (`min_bytes`), is not
    already digested at this size (the ledger), and has stopped growing
    (the tracker). Only the enumeration of candidates differs per harness,
    so it stays in the loops and the decision lives here.
    """

    tracker: SettleTracker
    ledger: DigestLedger
    min_bytes: int = 0
    min_delta: int = 0

    def hydrate_from_ledger(self) -> int:
        """Pre-mark every known session as fired. Returns how many.

        `normalize_key` keeps a `opencode://<id>` key a string: `Path`
        collapses the double slash, and the mangled key would never match
        the one the ledger stores.
        """
        for key, rec in self.ledger.sessions.items():
            self.tracker.hydrate(normalize_key(key), rec.last_digested_size)
        return len(self.ledger.sessions)

    def due(self, key: Path | str, size: int) -> bool:
        if size < self.min_bytes:
            return False
        if self.ledger.should_skip(key, size=size, min_delta=self.min_delta):
            self.tracker.hydrate(key, size)
            return False
        return self.tracker.tick(key, size)

    def mark_fired(self, key: Path | str, size: int) -> None:
        self.tracker.mark_fired(key, size)


def _process(
    gate: SettleGate,
    key: Path | str,
    size: int,
    *,
    label: str,
    run: Callable[[], bool | None],
    cooldown: float,
) -> bool:
    """Digest one candidate if it is due. True means: abandon this pass.

    A rate limit is the one failure that stops the whole sweep — the next
    session would hit the same wall. The candidate is left unfired, so it
    is retried once the window reopens.

    `run` returning False means the work did not settle — a refused roll-up,
    say — so the candidate stays unfired for the next pass. Marking it fired
    would bury it for good: a settled session never changes size again, so
    the tracker would suppress it forever even though the ledger claim was
    deliberately left uncommitted to schedule exactly that retry.
    """
    if not gate.due(key, size):
        return False
    try:
        if run() is not False:
            gate.mark_fired(key, size)
    except SagentRateLimitError as exc:
        print(
            f"[sagent] rate limit hit; sleeping "
            f"{cooldown:.0f}s before resuming. "
            f"({exc})"
        )
        time.sleep(cooldown)
        return True
    except Exception as exc:
        print(f"[sagent] digest error on {label}: {exc}")
    return False


def _safe_size(path: Path) -> int:
    try:
        return path.stat().st_size if path.exists() else 0
    except FileNotFoundError:
        return 0


# ---------------------------------------------------------------------------
# Watch loops
# ---------------------------------------------------------------------------


def watch(
    target: Path,
    on_change: Callable[[Path], bool | None],
    interval: float = 2.0,
    quiet_seconds: float = DEFAULT_QUIET_SECONDS,
) -> None:
    """Poll a JSONL file; fire on_change after writes settle for quiet_seconds."""
    print(f"[sagent] watching {target} (idle threshold: {quiet_seconds}s)")
    tracker = SettleTracker(quiet_seconds=quiet_seconds)
    while True:
        size = _safe_size(target)
        if tracker.tick(target, size):
            try:
                if on_change(target) is not False:
                    tracker.mark_fired(target, size)
            except Exception as exc:
                print(f"[sagent] digest error: {exc}")
        time.sleep(interval)


def watch_project(
    project_dir: Path,
    on_change: Callable[[Path], bool | None],
    interval: float = 2.0,
    quiet_seconds: float = DEFAULT_QUIET_SECONDS,
) -> None:
    """Follow whichever session is most recent in project_dir."""
    print(f"[sagent] watching project dir {project_dir} (idle: {quiet_seconds}s)")
    tracker = SettleTracker(quiet_seconds=quiet_seconds)
    current: Path | None = None
    while True:
        latest = latest_session(project_dir)
        if latest != current:
            print(f"[sagent] active session: {latest}")
            if current is not None:
                tracker.reset(current)
            current = latest
        if current is None:
            time.sleep(interval)
            continue
        try:
            size = current.stat().st_size
        except FileNotFoundError:
            time.sleep(interval)
            continue
        if tracker.tick(current, size):
            try:
                if on_change(current) is not False:
                    tracker.mark_fired(current, size)
            except Exception as exc:
                print(f"[sagent] digest error: {exc}")
        time.sleep(interval)


@dataclass(frozen=True)
class OpencodeTarget:
    """A settled opencode session, with everything a digest needs.

    The digest re-reads nothing the sweep already read: the row carries the
    id and the cwd, and `size` is the byte total the settle decision used.
    """

    row: SessionRow
    db_path: Path
    size: int

    @property
    def session_id(self) -> str:
        return self.row.session_id

    @property
    def ledger_key(self) -> str:
        return self.row.ledger_key


def _claude_pass(
    *,
    gate: SettleGate,
    root: Path,
    on_change: Callable[[Path], bool | None],
    cooldown: float,
) -> bool:
    """One sweep over the Claude Code session files. True = rate limited.

    Real projects come before scratchpads so the high-value cumulative
    digests don't get starved behind thousands of one-off sessions.
    """
    from .rollup import is_scratchpad

    projs = [p for p in root.iterdir() if p.is_dir()]
    projs.sort(key=lambda p: (is_scratchpad(p.name), p.name))
    for proj in projs:
        for sess in sorted(proj.glob("*.jsonl")):
            try:
                size = sess.stat().st_size
            except FileNotFoundError:
                continue
            if _process(
                gate,
                sess,
                size,
                label=str(sess),
                run=lambda s=sess: on_change(s),
                cooldown=cooldown,
            ):
                return True
    return False


def _opencode_pass(
    *,
    gate: SettleGate,
    db: Path,
    on_change: Callable[[OpencodeTarget], bool | None],
    cooldown: float,
) -> bool:
    """One sweep over the opencode database. True = rate limited.

    Liveness comes from the rows, never from the database file's mtime:
    while an opencode server holds the connection the writes sit in the
    WAL, and the main file's mtime lags real activity by minutes (measured
    on this host: 8). A stat-based watcher would fire long after a session
    settled, or never. `SUM(LENGTH(part.data))` grows with the session, so
    it drives the same settle logic `st_size` does.

    Child sessions never appear here: `list_sessions` filters them out in
    SQL, because a subagent's result is already inlined in its parent.
    """
    from .rollup import is_scratchpad

    try:
        rows = opencode.list_sessions(db)
    except Exception as exc:
        print(f"[sagent] opencode: cannot read {db}: {exc}")
        return False
    rows.sort(key=lambda r: (is_scratchpad(r.project_key), r.project_key))
    for row in rows:
        try:
            size = opencode.session_bytes(db, row.session_id)
        except Exception as exc:
            print(f"[sagent] opencode: cannot size {row.session_id}: {exc}")
            continue
        target = OpencodeTarget(row=row, db_path=Path(db), size=size)
        if _process(
            gate,
            row.ledger_key,
            size,
            label=row.ledger_key,
            run=lambda t=target: on_change(t),
            cooldown=cooldown,
        ):
            return True
    return False


def _resolve_db(db_path: Path | str | None) -> Path | None:
    return Path(db_path) if db_path is not None else opencode.find_database()


def watch_all(
    on_change: Callable[[Path], bool | None],
    root: Path = CLAUDE_PROJECTS,
    interval: float = 2.0,
    quiet_seconds: float = DEFAULT_QUIET_SECONDS,
    min_bytes: int = 5_000,
    min_delta: int = 0,
    ledger: DigestLedger | None = None,
    rate_limit_cooldown: float = 1800.0,
    on_opencode: Callable[[OpencodeTarget], None] | None = None,
    db_path: Path | str | None = None,
    db_poll_seconds: float = DEFAULT_DB_POLL_SECONDS,
) -> None:
    """Watch every project under root, digesting each session when it settles.

    Sessions already digested at their current size (per the ledger) are
    skipped on startup — no re-digest cost across service restarts.

    Real projects are processed before scratchpads each pass so the high-value
    cumulative digests don't get starved behind thousands of one-off sessions.

    On a SagentRateLimitError from on_change, the loop sleeps
    `rate_limit_cooldown` seconds and skips marking the path as fired so it
    is retried after the window reopens.

    Pass `on_opencode` to watch the opencode database in the same loop.
    One process, one ledger, one rate-limit cooldown and no threads: the
    two harnesses compete for the same API budget, so serialising them is
    the point. The database is swept on its own slower clock
    (`db_poll_seconds`) and is re-discovered while it is missing, so
    installing opencode later needs no restart.
    """
    if ledger is None:
        ledger = NullLedger()

    print(
        f"[sagent] watch-all: {root} "
        f"(skip < {min_bytes} bytes, idle: {quiet_seconds}s, "
        f"min-delta: {min_delta}, rate-limit cooldown: {rate_limit_cooldown}s)"
    )

    gate = SettleGate(
        tracker=SettleTracker(quiet_seconds=quiet_seconds),
        ledger=ledger,
        min_bytes=min_bytes,
        min_delta=min_delta,
    )
    hydrated = gate.hydrate_from_ledger()
    if hydrated:
        print(f"[sagent] hydrated {hydrated} session(s) from state")

    db = _resolve_db(db_path) if on_opencode is not None else None
    if on_opencode is not None:
        print(
            f"[sagent] watch-all: opencode db {db or '(not found yet)'} "
            f"(every {db_poll_seconds:.0f}s)"
        )
    next_db_poll = 0.0

    while True:
        rate_limited = False
        if root.exists():
            rate_limited = _claude_pass(
                gate=gate,
                root=root,
                on_change=on_change,
                cooldown=rate_limit_cooldown,
            )
        if (
            on_opencode is not None
            and not rate_limited
            and time.monotonic() >= next_db_poll
        ):
            db = db or _resolve_db(db_path)
            if db is not None:
                _opencode_pass(
                    gate=gate,
                    db=db,
                    on_change=on_opencode,
                    cooldown=rate_limit_cooldown,
                )
            next_db_poll = time.monotonic() + db_poll_seconds
        time.sleep(interval)


def watch_opencode(
    on_change: Callable[[OpencodeTarget], bool | None],
    *,
    db_path: Path | str | None = None,
    interval: float = DEFAULT_DB_POLL_SECONDS,
    quiet_seconds: float = DEFAULT_QUIET_SECONDS,
    min_bytes: int = 5_000,
    min_delta: int = 0,
    ledger: DigestLedger | None = None,
    rate_limit_cooldown: float = 1800.0,
) -> None:
    """Watch the opencode database alone, digesting sessions as they settle.

    The opencode half of `watch_all`, for a host that runs only opencode or
    for a second service unit. Prefer `watch_all(..., on_opencode=...)` when
    both harnesses run: one loop shares one rate-limit cooldown.
    """
    if ledger is None:
        ledger = NullLedger()

    print(
        f"[sagent] watch-opencode: {db_path or opencode.find_database() or '(no db)'} "
        f"(skip < {min_bytes} bytes, idle: {quiet_seconds}s, "
        f"min-delta: {min_delta}, rate-limit cooldown: {rate_limit_cooldown}s)"
    )

    gate = SettleGate(
        tracker=SettleTracker(quiet_seconds=quiet_seconds),
        ledger=ledger,
        min_bytes=min_bytes,
        min_delta=min_delta,
    )
    hydrated = gate.hydrate_from_ledger()
    if hydrated:
        print(f"[sagent] hydrated {hydrated} session(s) from state")

    db = _resolve_db(db_path)
    while True:
        db = db or _resolve_db(db_path)
        if db is not None:
            _opencode_pass(
                gate=gate,
                db=db,
                on_change=on_change,
                cooldown=rate_limit_cooldown,
            )
        time.sleep(interval)
