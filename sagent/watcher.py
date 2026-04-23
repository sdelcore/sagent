from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"


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
    quiet_seconds: float = 3.0,
) -> None:
    """Poll a JSONL file; fire on_change after writes settle for quiet_seconds.

    Settling avoids re-running the digest on every append during an active turn.
    """
    last_size = -1
    last_change_at = 0.0
    fired_for_size = -1
    print(f"[sagent] watching {target}")
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
    quiet_seconds: float = 3.0,
) -> None:
    """Follow whichever session is most recent in project_dir."""
    print(f"[sagent] watching project dir {project_dir}")
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
    quiet_seconds: float = 3.0,
) -> None:
    """Watch every project under root, digesting each session when it settles.

    Tracks per-file sizes; fires on_change once per file after writes settle.
    Picks up new project directories and new sessions as they appear.
    """
    print(f"[sagent] watch-all: {root}")
    last_size: dict[Path, int] = {}
    last_change_at: dict[Path, float] = {}
    fired_for_size: dict[Path, int] = {}
    while True:
        if not root.exists():
            time.sleep(interval)
            continue
        now = time.monotonic()
        for proj in root.iterdir():
            if not proj.is_dir():
                continue
            for sess in proj.glob("*.jsonl"):
                try:
                    size = sess.stat().st_size
                except FileNotFoundError:
                    continue
                prev = last_size.get(sess, -1)
                if size != prev:
                    last_size[sess] = size
                    last_change_at[sess] = now
                elif (
                    size > 0
                    and fired_for_size.get(sess, -1) != size
                    and now - last_change_at.get(sess, now) >= quiet_seconds
                ):
                    try:
                        on_change(sess)
                        fired_for_size[sess] = size
                    except Exception as exc:
                        print(f"[sagent] digest error on {sess}: {exc}")
        time.sleep(interval)
