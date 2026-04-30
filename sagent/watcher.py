from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .rate import SagentRateLimitError
from .state import DigestLedger, NullLedger

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"

DEFAULT_QUIET_SECONDS = 300.0  # 5 minutes; "summarize when a session goes idle"


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
    """Per-path "has this stopped growing for `quiet_seconds`?" tracker.

    The three watch loops differ in *which* paths to poll; the bookkeeping
    is the same — record sizes, note the moment a path's size last changed,
    and fire exactly once per (path, size) pair after the quiet window.

    Use:
      tracker = SettleTracker(quiet_seconds=300)
      tracker.hydrate(prev_path, prev_size)  # suppress re-fire across restarts
      ...
      if tracker.tick(path, current_size):
          on_change(path)
          tracker.mark_fired(path, current_size)
    """

    quiet_seconds: float
    _last_size: dict[Path, int] = field(default_factory=dict)
    _last_change_at: dict[Path, float] = field(default_factory=dict)
    _fired_for_size: dict[Path, int] = field(default_factory=dict)

    def hydrate(self, path: Path, size: int) -> None:
        """Pre-mark `path` at `size` as already fired.

        Used on watcher startup so paths the ledger already knows about
        don't re-fire just because the polling loop is starting fresh.
        """
        self._fired_for_size[path] = size
        self._last_size[path] = size

    def tick(self, path: Path, size: int, *, now: float | None = None) -> bool:
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

    def mark_fired(self, path: Path, size: int) -> None:
        self._fired_for_size[path] = size

    def reset(self, path: Path) -> None:
        """Forget all state for `path`. Used by watch_project when the
        active session changes."""
        self._last_size.pop(path, None)
        self._last_change_at.pop(path, None)
        self._fired_for_size.pop(path, None)


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
    on_change: Callable[[Path], None],
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
                on_change(target)
                tracker.mark_fired(target, size)
            except Exception as exc:
                print(f"[sagent] digest error: {exc}")
        time.sleep(interval)


def watch_project(
    project_dir: Path,
    on_change: Callable[[Path], None],
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
                on_change(current)
                tracker.mark_fired(current, size)
            except Exception as exc:
                print(f"[sagent] digest error: {exc}")
        time.sleep(interval)


def watch_all(
    on_change: Callable[[Path], None],
    root: Path = CLAUDE_PROJECTS,
    interval: float = 2.0,
    quiet_seconds: float = DEFAULT_QUIET_SECONDS,
    min_bytes: int = 5_000,
    min_delta: int = 0,
    ledger: DigestLedger | None = None,
    rate_limit_cooldown: float = 1800.0,
) -> None:
    """Watch every project under root, digesting each session when it settles.

    Sessions already digested at their current size (per the ledger) are
    skipped on startup — no re-digest cost across service restarts.

    Real projects are processed before scratchpads each pass so the high-value
    cumulative digests don't get starved behind thousands of one-off sessions.

    On a SagentRateLimitError from on_change, the loop sleeps
    `rate_limit_cooldown` seconds and skips marking the path as fired so it
    is retried after the window reopens.
    """
    from .rollup import is_scratchpad

    if ledger is None:
        ledger = NullLedger()

    print(
        f"[sagent] watch-all: {root} "
        f"(skip < {min_bytes} bytes, idle: {quiet_seconds}s, "
        f"min-delta: {min_delta}, rate-limit cooldown: {rate_limit_cooldown}s)"
    )

    tracker = SettleTracker(quiet_seconds=quiet_seconds)
    for path_str, rec in ledger.sessions.items():
        tracker.hydrate(Path(path_str), rec.last_digested_size)
    if ledger.sessions:
        print(f"[sagent] hydrated {len(ledger.sessions)} session(s) from state")

    while True:
        if not root.exists():
            time.sleep(interval)
            continue
        # Real projects first, scratchpads last
        projs = [p for p in root.iterdir() if p.is_dir()]
        projs.sort(key=lambda p: (is_scratchpad(p.name), p.name))
        rate_limited = False
        for proj in projs:
            if rate_limited:
                break
            for sess in sorted(proj.glob("*.jsonl")):
                try:
                    size = sess.stat().st_size
                except FileNotFoundError:
                    continue
                if size < min_bytes:
                    continue
                if ledger.should_skip(sess, size=size, min_delta=min_delta):
                    tracker.hydrate(sess, size)
                    continue
                if not tracker.tick(sess, size):
                    continue
                try:
                    on_change(sess)
                    tracker.mark_fired(sess, size)
                except SagentRateLimitError as exc:
                    print(
                        f"[sagent] rate limit hit; sleeping "
                        f"{rate_limit_cooldown:.0f}s before resuming. "
                        f"({exc})"
                    )
                    time.sleep(rate_limit_cooldown)
                    rate_limited = True
                    break
                except Exception as exc:
                    print(f"[sagent] digest error on {sess}: {exc}")
        time.sleep(interval)
