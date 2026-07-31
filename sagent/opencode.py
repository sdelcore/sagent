"""Ingest opencode sessions into sagent's Claude Code data model.

Opencode keeps every session in one SQLite database instead of per-session
JSONL files, and that database is a private, unversioned schema. So this
module splits the job in two, and deliberately keeps the fragile half tiny:

  - **Discovery and settle detection** read the database directly, but only
    four columns of one table (`session`) plus a byte total over `part`.
    Read-only, `mode=ro`, never a write. The `event` table is a duplicate
    snapshot log and is never read.
  - **Content** comes from `opencode --pure export <id>`, the public CLI
    contract, which returns `{"info": ..., "messages": [{"info", "parts"}]}`.

The database's own mtime lags real activity by minutes while a server holds
the connection, so it is never a liveness signal. `session_bytes` is: it
sums the stored part payloads, grows monotonically like `st_size`, and so
feeds the ledger, the settle tracker and the byte thresholds unchanged.

Child sessions (`parent_id IS NOT NULL`) are skipped entirely. A subagent's
full result is already inlined in the parent's `task` tool part, so nothing
is lost, and every opencode digest then has the same shape as a Claude Code
one.

The mapping targets `parser.Event` / `parser.Session`, so every downstream
module — transcript building, session markdown, roll-up — works unchanged.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .parser import Event, Session

HARNESS = "opencode"

LEDGER_SCHEME = "opencode"

# Install channels other than `latest` write `opencode-<channel>.db` beside
# the default file, so discovery globs rather than naming one file.
DB_GLOB = "opencode*.db"
DEFAULT_DB_NAME = "opencode.db"

EXPORT_TIMEOUT = 180.0

_BINARY_NAME = "opencode"
_BUNDLED_BINARY = Path.home() / ".opencode" / "bin" / _BINARY_NAME

# The encrypted reasoning blob is ~95% of a reasoning part's bytes and carries
# no readable signal — the provider needs it, a digest never does.
_REASONING_BLOB_KEY = "reasoningEncryptedContent"


class OpencodeError(RuntimeError):
    """Base class for every failure this module raises deliberately."""


class OpencodeBinaryError(OpencodeError):
    """The `opencode` executable could not be found."""


class OpencodeExportError(OpencodeError):
    """`opencode --pure export` failed, timed out, or returned non-JSON."""


@dataclass(frozen=True)
class SessionRow:
    """One top-level session, as seen through the four columns D1 allows.

    `directory` is the absolute cwd and is the project identity: `project_id`
    is not usable because every non-git directory collapses onto the sentinel
    project `global`.
    """

    session_id: str
    directory: str
    project_id: str
    time_updated: int

    @property
    def ledger_key(self) -> str:
        return ledger_key(self.session_id)

    @property
    def project_key(self) -> str:
        return project_key(self.directory)


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------


def ledger_key(session_id: str) -> str:
    """Ledger key for an opencode session: `opencode://<session_id>`.

    Claude Code sessions key on their JSONL path. Opencode sessions have no
    file, so they take a URI form instead; the ledger stores it verbatim and
    its prune pass skips anything with a scheme.
    """
    return f"{LEDGER_SCHEME}://{session_id}"


def project_key(directory: str | None) -> str:
    """Project key for a session cwd, matching the Claude Code key exactly.

    Claude Code names its project directories by replacing `/` with `-` in the
    cwd. Applying the same transform to `session.directory` means both
    harnesses land on one project digest for one working tree.
    """
    if not directory:
        return ""
    flat = str(directory).rstrip("/").replace("/", "-")
    # Imported here, not at module scope: the pipeline will import this
    # module once opencode ingestion is wired in, and a top-level import
    # would then be a cycle.
    from .pipeline import clean_project_name

    return clean_project_name(flat)


# ---------------------------------------------------------------------------
# Database discovery
# ---------------------------------------------------------------------------


def data_dir() -> Path:
    """The opencode data directory, honouring `$XDG_DATA_HOME`."""
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return base / "opencode"


def find_databases() -> list[Path]:
    """Every opencode database on this host, preferred one first.

    `$OPENCODE_DB` wins when set; it may be `:memory:`, an absolute path, or
    a bare filename relative to the data directory. Otherwise the data
    directory is globbed, with the default channel's `opencode.db` first.
    Missing files and an unreadable data directory both yield an empty list,
    so every returned path is one that can actually be opened.
    """
    override = os.environ.get("OPENCODE_DB")
    if override:
        path = _resolve_db_override(override)
        if str(path) == ":memory:" or str(path).startswith("file:"):
            return [path]
        return [path] if path.is_file() else []
    try:
        found = sorted(p for p in data_dir().glob(DB_GLOB) if p.is_file())
    except OSError:
        return []
    found.sort(key=lambda p: (p.name != DEFAULT_DB_NAME, p.name))
    return found


def find_database() -> Path | None:
    """The opencode database to read, or None when opencode is not installed.

    Never raises: a host without opencode is the normal case, not an error.
    """
    found = find_databases()
    return found[0] if found else None


def _resolve_db_override(value: str) -> Path:
    if value == ":memory:" or value.startswith("file:"):
        return Path(value)
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return data_dir() / path


# ---------------------------------------------------------------------------
# Read-only queries
# ---------------------------------------------------------------------------


def _read_only_uri(target: str) -> str:
    """A `file:` URI for `target` that always carries `mode=ro`.

    A `$OPENCODE_DB` override may already be a URI, and one written without
    `mode=ro` would open the live database read-write. sagent never writes to
    opencode's database, so the mode is forced rather than trusted.
    """
    if not target.startswith("file:"):
        return f"file:{target}?mode=ro"
    if re.search(r"[?&]mode=", target):
        return re.sub(r"([?&]mode=)[^&]*", r"\1ro", target, count=1)
    return target + ("&" if "?" in target else "?") + "mode=ro"


@contextmanager
def _connect(db: Path | str) -> Iterator[sqlite3.Connection]:
    """Open `db` read-only. sagent never writes to opencode's database."""
    target = str(db)
    if target == ":memory:":
        conn = sqlite3.connect(target)
    else:
        conn = sqlite3.connect(_read_only_uri(target), uri=True)
    with closing(conn):
        yield conn


def list_sessions(db: Path | str) -> list[SessionRow]:
    """Top-level sessions, oldest activity first.

    Child sessions are filtered out in SQL: a subagent transcript is already
    inlined in its parent's `task` tool output, so digesting it separately
    would only duplicate content and split one effort across two documents.
    """
    sql = (
        "SELECT id, directory, project_id, time_updated "
        "FROM session WHERE parent_id IS NULL ORDER BY time_updated"
    )
    with _connect(db) as conn:
        rows = conn.execute(sql).fetchall()
    return [
        SessionRow(
            session_id=str(r[0]),
            directory=str(r[1] or ""),
            project_id=str(r[2] or ""),
            time_updated=int(r[3] or 0),
        )
        for r in rows
    ]


def session_bytes(db: Path | str, session_id: str) -> int:
    """Total stored size of a session's parts — the `st_size` stand-in.

    Only monotonic growth matters here, so SQLite's character-count `LENGTH`
    is as good as a byte count and much cheaper than reading the payloads.
    """
    sql = "SELECT COALESCE(SUM(LENGTH(data)), 0) FROM part WHERE session_id = ?"
    with _connect(db) as conn:
        row = conn.execute(sql, (session_id,)).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


# ---------------------------------------------------------------------------
# Content export
# ---------------------------------------------------------------------------


def resolve_binary() -> Path:
    """Locate the `opencode` executable.

    Order: `$OPENCODE_BIN`, the installer's `~/.opencode/bin/opencode`, then
    `$PATH`.
    """
    override = os.environ.get("OPENCODE_BIN")
    if override:
        return Path(override).expanduser()
    if _BUNDLED_BINARY.exists():
        return _BUNDLED_BINARY
    found = shutil.which(_BINARY_NAME)
    if found:
        return Path(found)
    raise OpencodeBinaryError(
        f"{_BINARY_NAME} not found (checked $OPENCODE_BIN, "
        f"{_BUNDLED_BINARY}, and $PATH)"
    )


def export_session(session_id: str, *, timeout: float = EXPORT_TIMEOUT) -> dict:
    """Return the parsed `opencode --pure export <id>` payload.

    `--pure` is mandatory, not cosmetic: without it loaded plugins print
    their own banners to stdout (e.g. "[opencode-litellm] Discovered 15
    models…"), which lands in front of the JSON and makes the payload
    unparseable.

    stdout is spooled to a file rather than a pipe. The opencode binary
    exits without draining its own stdout, so a pipe silently truncates the
    export at the 64KB buffer — measured on this host, a 618KB export
    arrived as 65490 bytes and failed to parse mid-string. Only sessions
    smaller than the buffer survived, which is almost none of the real ones.
    A file has no such limit.

    The export covers exactly one session; it never includes children.
    """
    binary = resolve_binary()
    cmd = [str(binary), "--pure", "export", session_id]
    with tempfile.TemporaryDirectory(prefix="sagent-opencode-") as tmpdir:
        spool = Path(tmpdir) / "export.json"
        try:
            with spool.open("wb") as sink:
                result = subprocess.run(
                    cmd,
                    stdout=sink,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=timeout,
                )
        except subprocess.TimeoutExpired as exc:
            raise OpencodeExportError(
                f"export of {session_id} timed out after {timeout:.0f}s"
            ) from exc
        except OSError as exc:
            raise OpencodeExportError(f"could not run {binary}: {exc}") from exc

        if result.returncode != 0:
            detail = (result.stderr or "").strip()[-500:]
            raise OpencodeExportError(
                f"export of {session_id} exited {result.returncode}: {detail}"
            )
        # `errors="replace"` on purpose: one undecodable byte inside a tool
        # output must not cost the whole session digest.
        raw = spool.read_text(encoding="utf-8", errors="replace")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        head = raw.strip()[:200]
        raise OpencodeExportError(
            f"export of {session_id} returned non-JSON in {len(raw)} chars "
            f"(plugin output on stdout?): {head!r}"
        ) from exc
    if not isinstance(payload, dict):
        raise OpencodeExportError(
            f"export of {session_id} returned {type(payload).__name__}, "
            "expected an object"
        )
    return payload


# ---------------------------------------------------------------------------
# Mapping onto parser.Session / parser.Event
# ---------------------------------------------------------------------------


def to_iso(ms: Any) -> str | None:
    """Convert opencode's epoch milliseconds to the ISO form parser uses.

    Everything downstream — `Session.date_prefix`, the transcript's clock
    column, the front-matter timestamps — reads an ISO 8601 UTC string, so
    the conversion happens once, here.
    """
    if isinstance(ms, bool) or not isinstance(ms, (int, float)):
        return None
    if ms <= 0:
        return None
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(ms) % 1000:03d}Z"


def strip_reasoning_blob(part: dict) -> dict:
    """Copy a reasoning part without the provider's encrypted payload.

    Measured on this host: 397KB of base64 wrapping 19KB of readable text.
    """
    scrubbed = copy.deepcopy(part)
    meta = scrubbed.get("metadata")
    if isinstance(meta, dict):
        for value in meta.values():
            if isinstance(value, dict):
                value.pop(_REASONING_BLOB_KEY, None)
    return scrubbed


def tool_output(state: dict) -> str:
    """Readable output of a completed tool part.

    Large outputs are offloaded to `tool-output/<id>` and only summarised
    inline. Those files are pruned independently of the database, so a
    missing one is expected: it yields an empty output and never an error.
    """
    if not isinstance(state, dict):
        return ""
    meta = state.get("metadata")
    path = meta.get("outputPath") if isinstance(meta, dict) else None
    if isinstance(path, str) and path:
        try:
            return Path(path).read_text()
        except OSError:
            return ""
    out = state.get("output")
    return out if isinstance(out, str) else ""


def _part_events(
    part: dict,
    *,
    role: str,
    message_id: str,
    fallback_ts: str | None,
) -> Iterator[Event]:
    ptype = part.get("type")
    uuid = str(part.get("id") or "")
    time_block = part.get("time")
    ts = to_iso(time_block.get("start")) if isinstance(time_block, dict) else None
    ts = ts or fallback_ts

    if ptype == "text":
        # A user message carries no text of its own; the prompt is a part.
        text = (part.get("text") or "").strip()
        if not text:
            return
        kind = "user_prompt" if role == "user" else "assistant_text"
        yield Event(kind, uuid, message_id, ts, text=text, raw=part)

    elif ptype == "reasoning":
        text = (part.get("text") or "").strip()
        if not text:
            return
        yield Event(
            "assistant_thinking",
            uuid,
            message_id,
            ts,
            text=text,
            raw=strip_reasoning_blob(part),
        )

    elif ptype == "tool":
        # One opencode part holds both the call and its result, so it maps to
        # a tool_use plus — on failure only — a tool_result. A successful
        # result is dropped by every consumer anyway.
        state = part.get("state")
        state = state if isinstance(state, dict) else {}
        inp = state.get("input")
        call_id = part.get("callID")
        yield Event(
            "tool_use",
            uuid,
            message_id,
            ts,
            tool_name=part.get("tool"),
            tool_input=inp if isinstance(inp, dict) else {},
            tool_use_id=call_id,
            raw=part,
        )
        if state.get("status") == "error":
            err = state.get("error")
            text = err if isinstance(err, str) else tool_output(state)
            yield Event(
                "tool_result",
                f"{uuid}:error" if uuid else "",
                message_id,
                ts,
                text=text,
                tool_use_id=call_id,
                is_error=True,
                raw=part,
            )

    # step-start, step-finish, patch and compaction are bookkeeping: they
    # carry a snapshot hash or a token count, never a fact worth digesting.


def iter_events(payload: dict) -> Iterator[Event]:
    """Events of one exported session, in transcript order.

    Order is the export's own message-then-part order, which is append-only.
    The ledger stores an event index for incremental digests, so a stable
    order is a correctness requirement, not a nicety.
    """
    for msg in payload.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        info = msg.get("info")
        info = info if isinstance(info, dict) else {}
        role = str(info.get("role") or "")
        message_id = str(info.get("id") or "")
        time_block = info.get("time")
        created = (
            to_iso(time_block.get("created")) if isinstance(time_block, dict) else None
        )
        for part in msg.get("parts") or []:
            if isinstance(part, dict):
                yield from _part_events(
                    part,
                    role=role,
                    message_id=message_id,
                    fallback_ts=created,
                )


def build_session(payload: dict, *, source_path: Path | None = None) -> Session:
    """Map an export payload onto `parser.Session`.

    `git_branch` is always None: opencode records no branch anywhere — the
    workspace table is empty and `workspace_id` is NULL on every row.

    `source_path` is what the digest's front matter cites as its source. It
    is the database, since there is no per-session file; callers that know
    which database they read should pass it. Without one the conventional
    database path stands in — never the ledger key, because `Path` collapses
    its double slash into `opencode:/<id>`, and a caller that later derived
    a key from `Session.path` would silently write a record no lookup could
    ever match.
    """
    info = payload.get("info")
    info = info if isinstance(info, dict) else {}
    session_id = str(info.get("id") or "")
    directory = info.get("directory")
    return Session(
        session_id=session_id,
        path=Path(source_path) if source_path else data_dir() / DEFAULT_DB_NAME,
        events=list(iter_events(payload)),
        cwd=str(directory) if directory else None,
        git_branch=None,
    )


def load_session(
    session_id: str,
    *,
    db_path: Path | str | None = None,
    timeout: float = EXPORT_TIMEOUT,
) -> Session:
    """Export one opencode session and return it as a `parser.Session`.

    The opencode counterpart of `parser.load_session`.
    """
    payload = export_session(session_id, timeout=timeout)
    source = db_path if db_path is not None else find_database()
    return build_session(payload, source_path=Path(source) if source else None)
