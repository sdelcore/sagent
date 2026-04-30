"""Persistent record of what's been digested.

The ledger answers two questions for the digest pipeline:
  - "have I already digested this session at this size?"  (the *skip* check)
  - "what was its prior event index, so I can run incrementally?"  (the
    *prior* lookup)

It also tracks per-project rollup count, so the pipeline knows when to
trigger a periodic full rebuild.

Two interfaces sit on top of the same underlying JSON file:

  - **Claim/commit** — the high-level seam used by the pipeline. Open a
    `DigestClaim`; if it returns `None`, the work is skippable. Do the
    work; on success, call `claim.commit(...)` to persist. A claim that
    is never committed leaves the ledger untouched, so a crash mid-digest
    causes a re-attempt next pass instead of a silent loss.
  - **Direct getters/setters** — `mark_digested`, `should_skip`, etc.
    Used by tools (prune, rollup) that don't fit the claim shape.

`NullLedger` is a no-op adapter with the same surface, so callers never
branch on `ledger is None`.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

CURRENT_VERSION = 1


def default_state_path() -> Path:
    base = os.environ.get("SAGENT_STATE")
    if base:
        return Path(base).expanduser()
    state_home = os.environ.get("XDG_STATE_HOME")
    if state_home:
        return Path(state_home) / "sagent" / "state.json"
    return Path.home() / ".local" / "state" / "sagent" / "state.json"


@dataclass
class SessionRecord:
    last_digested_size: int = 0
    last_event_index: int = 0
    last_digested_at: str = ""
    digest_count: int = 0


@dataclass
class ProjectRecord:
    last_rolled_up_session_id: str = ""
    last_rolled_up_at: str = ""
    rollup_count: int = 0


@dataclass
class DigestClaim:
    """A reservation to digest one session.

    Holds the prior `SessionRecord` (if any) so the caller can decide
    incremental vs. full and access `digest_count`. Call `commit()` after
    a successful or terminal-drop digest; skip the call to leave state
    untouched (e.g. on rate-limit or crash).
    """

    session_path: Path
    size: int
    prior: SessionRecord | None
    _ledger: "DigestLedger"

    def commit(self, *, event_index: int) -> None:
        self._ledger.mark_digested(
            self.session_path, size=self.size, event_index=event_index
        )
        self._ledger.save()


@dataclass
class RollupClaim:
    """A reservation to roll up one project's digests.

    `prior_count` is the previous `rollup_count` (used by the pipeline to
    decide on periodic full rebuilds). Call `commit(session_id=...)` after
    a successful rollup.
    """

    project_name: str
    prior_count: int
    _ledger: "DigestLedger"

    def commit(self, *, session_id: str) -> None:
        self._ledger.mark_rolled_up(self.project_name, session_id=session_id)
        self._ledger.save()


class DigestLedger:
    """Persistent per-session and per-project digest state.

    Single JSON file, atomic writes via temp-then-rename. One writer (the
    sagent process); no locking. Tolerant to a missing or corrupt file —
    falls back to empty state and overwrites on next save.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else default_state_path()
        self.sessions: dict[str, SessionRecord] = {}
        self.projects: dict[str, ProjectRecord] = {}
        self._loaded_version = CURRENT_VERSION
        self.load()

    # -----------------------------------------------------------------
    # High-level claim/commit API
    # -----------------------------------------------------------------

    def claim(
        self,
        session_path: Path | str,
        *,
        size: int,
        min_delta: int = 0,
        force: bool = False,
    ) -> DigestClaim | None:
        """Reserve a digest of `session_path` at `size`.

        Returns None if the session is already digested at >= this size
        (or the size delta is below `min_delta`). `force=True` always
        returns a claim, regardless of prior state.
        """
        if not force and self.should_skip(
            session_path, size=size, min_delta=min_delta
        ):
            return None
        return DigestClaim(
            session_path=Path(session_path),
            size=size,
            prior=self.get(session_path),
            _ledger=self,
        )

    def claim_rollup(self, project_name: str) -> RollupClaim:
        """Reserve a rollup pass for `project_name`. Always returns a claim."""
        prior = self.projects.get(project_name)
        return RollupClaim(
            project_name=project_name,
            prior_count=prior.rollup_count if prior else 0,
            _ledger=self,
        )

    # -----------------------------------------------------------------
    # Persistence
    # -----------------------------------------------------------------

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError):
            return
        self._loaded_version = data.get("version", 1)
        raw = data.get("sessions") or {}
        loaded: dict[str, SessionRecord] = {}
        for k, v in raw.items():
            if not isinstance(v, dict):
                continue
            loaded[k] = SessionRecord(
                last_digested_size=int(v.get("last_digested_size", 0)),
                last_event_index=int(v.get("last_event_index", 0)),
                last_digested_at=str(v.get("last_digested_at", "")),
                digest_count=int(v.get("digest_count", 0)),
            )
        self.sessions = loaded
        raw_p = data.get("projects") or {}
        loaded_p: dict[str, ProjectRecord] = {}
        for k, v in raw_p.items():
            if not isinstance(v, dict):
                continue
            loaded_p[k] = ProjectRecord(
                last_rolled_up_session_id=str(v.get("last_rolled_up_session_id", "")),
                last_rolled_up_at=str(v.get("last_rolled_up_at", "")),
                rollup_count=int(v.get("rollup_count", 0)),
            )
        self.projects = loaded_p

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": CURRENT_VERSION,
            "sessions": {k: asdict(v) for k, v in self.sessions.items()},
            "projects": {k: asdict(v) for k, v in self.projects.items()},
        }
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=str(self.path.parent),
            delete=False,
            prefix=".state-",
            suffix=".tmp",
            encoding="utf-8",
        ) as tmp:
            json.dump(data, tmp, indent=2, sort_keys=True)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_path = Path(tmp.name)
        os.replace(tmp_path, self.path)

    # -----------------------------------------------------------------
    # Low-level getters/setters (still used by tools that don't fit the
    # claim shape — prune, rollup-from-cli — and by claims internally)
    # -----------------------------------------------------------------

    def get(self, session_path: Path | str) -> SessionRecord | None:
        return self.sessions.get(str(session_path))

    def mark_digested(
        self,
        session_path: Path | str,
        *,
        size: int,
        event_index: int,
    ) -> SessionRecord:
        key = str(session_path)
        rec = self.sessions.setdefault(key, SessionRecord())
        rec.last_digested_size = size
        rec.last_event_index = event_index
        rec.last_digested_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        rec.digest_count += 1
        return rec

    def should_skip(
        self,
        session_path: Path | str,
        *,
        size: int,
        min_delta: int = 0,
    ) -> bool:
        """True if the session is already digested at >= this size, or if the
        delta since last digest is below min_delta.
        """
        rec = self.sessions.get(str(session_path))
        if rec is None:
            return False
        if rec.last_digested_size >= size:
            return True
        if min_delta > 0 and (size - rec.last_digested_size) < min_delta:
            return True
        return False

    def prune_missing(self, valid_paths: set[Path]) -> int:
        """Drop records for paths that no longer exist on disk. Returns count."""
        valid_strs = {str(p) for p in valid_paths}
        gone = [k for k in self.sessions if k not in valid_strs]
        for k in gone:
            del self.sessions[k]
        return len(gone)

    def get_project(self, project_name: str) -> ProjectRecord | None:
        return self.projects.get(project_name)

    def mark_rolled_up(self, project_name: str, *, session_id: str) -> ProjectRecord:
        rec = self.projects.setdefault(project_name, ProjectRecord())
        rec.last_rolled_up_session_id = session_id
        rec.last_rolled_up_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        rec.rollup_count += 1
        return rec


class NullLedger(DigestLedger):
    """Drop-in ledger for `--no-state` runs.

    Inherits the in-memory shape (so `.sessions`/`.projects` exist as empty
    dicts and the watcher's hydration loop is a no-op) but never reads or
    writes a file. Claims still work; commits update the in-memory dicts
    but `save()` is a no-op, so nothing leaks to disk.
    """

    def __init__(self) -> None:
        # Skip the parent __init__ entirely — no path, no load.
        self.path = Path(os.devnull)
        self.sessions = {}
        self.projects = {}
        self._loaded_version = CURRENT_VERSION

    def load(self) -> None:  # pragma: no cover - intentional no-op
        return

    def save(self) -> None:  # pragma: no cover - intentional no-op
        return
