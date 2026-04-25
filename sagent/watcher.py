from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

from .rate import SagentRateLimitError
from .state import StateStore

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


def watch(
    target: Path,
    on_change: Callable[[Path], None],
    interval: float = 2.0,
    quiet_seconds: float = DEFAULT_QUIET_SECONDS,
) -> None:
    """Poll a JSONL file; fire on_change after writes settle for quiet_seconds."""
    last_size = -1
    last_change_at = 0.0
    fired_for_size = -1
    print(f"[sagent] watching {target} (idle threshold: {quiet_seconds}s)")
    while True:
        try:
            size = target.stat().st_size if target.exists() else 0
        except FileNotFoundError:
            size = 0
        now = time.monotonic()
        if size != last_size:
            last_size = size
            last_change_at = now
        elif (
            size > 0
            and size != fired_for_size
            and now - last_change_at >= quiet_seconds
        ):
            try:
                on_change(target)
                fired_for_size = size
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
    current: Path | None = None
    last_size = -1
    last_change_at = 0.0
    fired_for_size = -1
    while True:
        latest = latest_session(project_dir)
        if latest != current:
            print(f"[sagent] active session: {latest}")
            current = latest
            last_size = -1
            fired_for_size = -1
        if current is None:
            time.sleep(interval)
            continue
        try:
            size = current.stat().st_size
        except FileNotFoundError:
            time.sleep(interval)
            continue
        now = time.monotonic()
        if size != last_size:
            last_size = size
            last_change_at = now
        elif (
            size > 0
            and size != fired_for_size
            and now - last_change_at >= quiet_seconds
        ):
            try:
                on_change(current)
                fired_for_size = size
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
    state: StateStore | None = None,
    rate_limit_cooldown: float = 1800.0,
) -> None:
    """Watch every project under root, digesting each session when it settles.

    If a `state` is provided, sessions already digested at their current size
    (per the state file) are skipped on startup — no re-digest cost across
    service restarts.

    Real projects are processed before scratchpads each pass so the high-value
    cumulative digests don't get starved behind thousands of one-off sessions.

    On a SagentRateLimitError from on_change, the loop sleeps
    `rate_limit_cooldown` seconds and skips updating fired_for_size so the
    session is retried after the window reopens.
    """
    from .rollup import is_scratchpad

    print(
        f"[sagent] watch-all: {root} "
        f"(skip < {min_bytes} bytes, idle: {quiet_seconds}s, "
        f"min-delta: {min_delta}, rate-limit cooldown: {rate_limit_cooldown}s)"
    )

    last_size: dict[Path, int] = {}
    last_change_at: dict[Path, float] = {}
    fired_for_size: dict[Path, int] = {}

    if state is not None:
        for path_str, rec in state.sessions.items():
            fired_for_size[Path(path_str)] = rec.last_digested_size
        if state.sessions:
            print(f"[sagent] hydrated {len(state.sessions)} session(s) from state")

    while True:
        if not root.exists():
            time.sleep(interval)
            continue
        now = time.monotonic()
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
                if state is not None and state.should_skip(
                    sess, size=size, min_delta=min_delta
                ):
                    fired_for_size[sess] = size
                    last_size[sess] = size
                    continue
                prev = last_size.get(sess, -1)
                if size != prev:
                    last_size[sess] = size
                    last_change_at[sess] = now
                elif (
                    fired_for_size.get(sess, -1) != size
                    and now - last_change_at.get(sess, now) >= quiet_seconds
                ):
                    try:
                        on_change(sess)
                        fired_for_size[sess] = size
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
