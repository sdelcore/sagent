from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from sagent import opencode
from sagent.opencode import (
    OpencodeBinaryError,
    OpencodeExportError,
    build_session,
    data_dir,
    export_session,
    find_database,
    find_databases,
    iter_events,
    ledger_key,
    list_sessions,
    load_session,
    project_key,
    resolve_binary,
    session_bytes,
    strip_reasoning_blob,
    to_iso,
    tool_output,
)
from sagent.pipeline import project_name_for_cwd

# The four columns D1 reads, plus enough of the real schema that a query
# written against opencode's own database runs unchanged here.
SCHEMA = """
CREATE TABLE session (
  id TEXT PRIMARY KEY,
  project_id TEXT,
  workspace_id TEXT,
  parent_id TEXT,
  slug TEXT,
  directory TEXT,
  path TEXT,
  title TEXT,
  version TEXT,
  agent TEXT,
  model TEXT,
  cost REAL,
  time_created INTEGER,
  time_updated INTEGER,
  time_compacting INTEGER,
  time_archived INTEGER
);
CREATE TABLE message (
  id TEXT PRIMARY KEY,
  session_id TEXT,
  time_created INTEGER,
  time_updated INTEGER,
  data TEXT
);
CREATE TABLE part (
  id TEXT PRIMARY KEY,
  message_id TEXT,
  session_id TEXT,
  time_created INTEGER,
  time_updated INTEGER,
  data TEXT
);
CREATE TABLE project (
  id TEXT PRIMARY KEY,
  worktree TEXT,
  vcs TEXT,
  name TEXT
);
"""


def _make_db(path: Path, sessions=(), parts=()) -> Path:
    """Write a throwaway database with the real column names.

    The user's live database is never read by a test; every fixture here is
    built from scratch under tmp_path.
    """
    conn = sqlite3.connect(str(path))
    with conn:
        conn.executescript(SCHEMA)
        conn.executemany(
            "INSERT INTO session (id, project_id, parent_id, directory, "
            "time_created, time_updated) VALUES (?, ?, ?, ?, ?, ?)",
            sessions,
        )
        conn.executemany(
            "INSERT INTO part (id, message_id, session_id, data) VALUES (?, ?, ?, ?)",
            parts,
        )
    conn.close()
    return path


def _isolate_env(monkeypatch, tmp_path: Path) -> Path:
    """Point discovery at tmp_path and clear any inherited overrides."""
    monkeypatch.delenv("OPENCODE_DB", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    share = tmp_path / "share" / "opencode"
    share.mkdir(parents=True)
    return share


# ---------------------------------------------------------------------------
# Database discovery
# ---------------------------------------------------------------------------


def test_data_dir_honours_xdg_data_home(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    assert data_dir() == tmp_path / "share" / "opencode"


def test_data_dir_falls_back_to_local_share(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert data_dir() == tmp_path / ".local" / "share" / "opencode"


def test_find_database_returns_none_when_absent(monkeypatch, tmp_path: Path):
    _isolate_env(monkeypatch, tmp_path)
    assert find_databases() == []
    assert find_database() is None


def test_find_database_returns_none_when_data_dir_missing(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("OPENCODE_DB", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "nothing-here"))
    assert find_database() is None


def test_find_databases_globs_non_default_channels(monkeypatch, tmp_path: Path):
    share = _isolate_env(monkeypatch, tmp_path)
    (share / "opencode-dev.db").touch()
    assert find_database() == share / "opencode-dev.db"


def test_find_databases_prefers_default_channel(monkeypatch, tmp_path: Path):
    share = _isolate_env(monkeypatch, tmp_path)
    (share / "opencode-beta.db").touch()
    (share / "opencode.db").touch()
    (share / "opencode-dev.db").touch()
    found = find_databases()
    assert found[0] == share / "opencode.db"
    assert set(found) == {
        share / "opencode.db",
        share / "opencode-beta.db",
        share / "opencode-dev.db",
    }


def test_find_databases_ignores_sidecars_and_other_files(monkeypatch, tmp_path: Path):
    share = _isolate_env(monkeypatch, tmp_path)
    (share / "opencode.db").touch()
    (share / "opencode.db-wal").touch()
    (share / "opencode.db-shm").touch()
    (share / "storage").mkdir()
    assert find_databases() == [share / "opencode.db"]


def test_opencode_db_env_overrides_discovery(monkeypatch, tmp_path: Path):
    share = _isolate_env(monkeypatch, tmp_path)
    (share / "opencode.db").touch()
    elsewhere = tmp_path / "custom.db"
    elsewhere.touch()
    monkeypatch.setenv("OPENCODE_DB", str(elsewhere))
    assert find_database() == elsewhere


def test_opencode_db_env_resolves_bare_name_under_data_dir(monkeypatch, tmp_path: Path):
    share = _isolate_env(monkeypatch, tmp_path)
    (share / "opencode-nightly.db").touch()
    monkeypatch.setenv("OPENCODE_DB", "opencode-nightly.db")
    assert find_database() == share / "opencode-nightly.db"


def test_opencode_db_env_pointing_at_missing_file_yields_none(
    monkeypatch, tmp_path: Path
):
    _isolate_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OPENCODE_DB", str(tmp_path / "gone.db"))
    assert find_database() is None


# ---------------------------------------------------------------------------
# Discovery order vs. the documented order (F10)
#
# `--opencode-db` promises name order, not write order. A user who reads the
# help must be able to predict which database sagent opens, so these pin the
# behaviour the help text describes — including the part it says sagent does
# NOT do.
# ---------------------------------------------------------------------------


def _touch_with_mtime(path: Path, when: float) -> Path:
    import os

    path.touch()
    os.utime(path, (when, when))
    return path


def test_find_databases_prefers_the_default_channel_over_the_newest_write(
    monkeypatch, tmp_path: Path
):
    """The help says the default channel wins, not the recent one."""
    share = _isolate_env(monkeypatch, tmp_path)
    _touch_with_mtime(share / "opencode.db", 1_000)
    _touch_with_mtime(share / "opencode-dev.db", 9_000)

    assert find_database() == share / "opencode.db"


def test_find_databases_orders_other_channels_by_name_not_by_mtime(
    monkeypatch, tmp_path: Path
):
    """With no default channel the tie-break is the name, as documented."""
    share = _isolate_env(monkeypatch, tmp_path)
    _touch_with_mtime(share / "opencode-zeta.db", 9_000)
    _touch_with_mtime(share / "opencode-beta.db", 1_000)

    assert find_databases() == [
        share / "opencode-beta.db",
        share / "opencode-zeta.db",
    ]
    assert find_database() == share / "opencode-beta.db"


def test_the_env_override_beats_the_default_channel_however_old(
    monkeypatch, tmp_path: Path
):
    share = _isolate_env(monkeypatch, tmp_path)
    _touch_with_mtime(share / "opencode.db", 9_000)
    elsewhere = _touch_with_mtime(tmp_path / "custom.db", 1_000)
    monkeypatch.setenv("OPENCODE_DB", str(elsewhere))

    assert find_databases() == [elsewhere]


def test_opencode_db_help_text_describes_the_real_discovery_order(capsys):
    """The help must not promise an ordering the code does not implement."""
    import pytest as _pytest

    from sagent import cli

    with _pytest.raises(SystemExit):
        cli.main(["digest-all", "--help"])
    help_text = " ".join(capsys.readouterr().out.split())

    assert "$OPENCODE_DB" in help_text
    assert "opencode.db" in help_text
    # The one thing discovery does not do, stated so a user knows to pass
    # the flag after an install-channel switch.
    assert "NOT the most recently written" in help_text


# ---------------------------------------------------------------------------
# Read-only session listing
# ---------------------------------------------------------------------------


def test_list_sessions_excludes_child_sessions(tmp_path: Path):
    db = _make_db(
        tmp_path / "o.db",
        sessions=[
            ("ses_parent", "proj", None, "/home/u/src/app", 1, 100),
            ("ses_child", "proj", "ses_parent", "/home/u/src/app", 2, 200),
            ("ses_other", "global", None, "/tmp/scratch", 3, 50),
        ],
    )
    rows = list_sessions(db)
    assert [r.session_id for r in rows] == ["ses_other", "ses_parent"]


def test_list_sessions_reads_the_four_columns(tmp_path: Path):
    db = _make_db(
        tmp_path / "o.db",
        sessions=[("ses_a", "global", None, "/tmp/work", 1, 1753660845123)],
    )
    (row,) = list_sessions(db)
    assert row.session_id == "ses_a"
    assert row.directory == "/tmp/work"
    assert row.project_id == "global"
    assert row.time_updated == 1753660845123


def test_list_sessions_tolerates_null_directory(tmp_path: Path):
    db = _make_db(tmp_path / "o.db", sessions=[("ses_a", None, None, None, 1, None)])
    (row,) = list_sessions(db)
    assert row.directory == ""
    assert row.project_id == ""
    assert row.time_updated == 0


def test_connection_is_read_only(tmp_path: Path):
    db = _make_db(tmp_path / "o.db", sessions=[("ses_a", "p", None, "/tmp/x", 1, 1)])
    with opencode._connect(db) as conn:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO session (id) VALUES ('ses_written')")
    assert [r.session_id for r in list_sessions(db)] == ["ses_a"]


# ---------------------------------------------------------------------------
# Byte total (the st_size stand-in)
# ---------------------------------------------------------------------------


def test_session_bytes_sums_part_data_lengths(tmp_path: Path):
    db = _make_db(
        tmp_path / "o.db",
        sessions=[("ses_a", "p", None, "/tmp/x", 1, 1)],
        parts=[
            ("prt_1", "msg_1", "ses_a", "12345"),
            ("prt_2", "msg_1", "ses_a", "678"),
            ("prt_3", "msg_2", "ses_b", "ignored-other-session"),
        ],
    )
    assert session_bytes(db, "ses_a") == 8


def test_session_bytes_is_zero_for_a_session_with_no_parts(tmp_path: Path):
    db = _make_db(tmp_path / "o.db", sessions=[("ses_a", "p", None, "/tmp/x", 1, 1)])
    assert session_bytes(db, "ses_a") == 0
    assert session_bytes(db, "ses_never_existed") == 0


def test_session_bytes_grows_monotonically_with_new_parts(tmp_path: Path):
    db = _make_db(
        tmp_path / "o.db",
        sessions=[("ses_a", "p", None, "/tmp/x", 1, 1)],
        parts=[("prt_1", "msg_1", "ses_a", "abcd")],
    )
    before = session_bytes(db, "ses_a")
    conn = sqlite3.connect(str(db))
    with conn:
        conn.execute(
            "INSERT INTO part (id, message_id, session_id, data) VALUES (?, ?, ?, ?)",
            ("prt_2", "msg_1", "ses_a", "efghij"),
        )
    conn.close()
    assert session_bytes(db, "ses_a") == before + 6


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------


def test_ledger_key_is_a_uri(tmp_path: Path):
    assert ledger_key("ses_abc") == "opencode://ses_abc"


def test_session_row_exposes_keys(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/tester")))
    db = _make_db(
        tmp_path / "o.db",
        sessions=[("ses_a", "p", None, "/home/tester/src/infra/nixos", 1, 1)],
    )
    (row,) = list_sessions(db)
    assert row.ledger_key == "opencode://ses_a"
    assert row.project_key == "src-infra-nixos"


def test_project_key_matches_the_claude_code_key(monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/tester")))
    cwd = "/home/tester/src/infra/nixos"
    assert project_key(cwd) == "src-infra-nixos"
    assert project_key(cwd) == project_name_for_cwd(cwd)


def test_project_key_strips_a_trailing_slash(monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/tester")))
    assert project_key("/home/tester/src/app/") == "src-app"


def test_project_key_of_a_non_home_directory(monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/tester")))
    assert project_key("/tmp/worktree-x") == "tmp-worktree-x"


def test_project_key_of_nothing_is_empty():
    assert project_key(None) == ""
    assert project_key("") == ""


# ---------------------------------------------------------------------------
# Epoch-ms conversion
# ---------------------------------------------------------------------------


def test_to_iso_converts_epoch_milliseconds():
    assert to_iso(1753660845123) == "2025-07-28T00:00:45.123Z"


def test_to_iso_keeps_three_digit_milliseconds():
    assert to_iso(1753747245000) == "2025-07-29T00:00:45.000Z"


def test_to_iso_rejects_non_timestamps():
    assert to_iso(None) is None
    assert to_iso(0) is None
    assert to_iso(-1) is None
    assert to_iso("1753660845123") is None
    assert to_iso(True) is None


# ---------------------------------------------------------------------------
# Part-type mapping
# ---------------------------------------------------------------------------


def _payload(*parts, role="assistant", created=1753660845123, info=None):
    return {
        "info": info if info is not None else {"id": "ses_a", "directory": "/tmp/x"},
        "messages": [
            {
                "info": {"id": "msg_1", "role": role, "time": {"created": created}},
                "parts": list(parts),
            }
        ],
    }


def test_text_part_of_a_user_message_is_a_prompt():
    events = list(iter_events(_payload({"id": "prt_1", "type": "text", "text": " go "}, role="user")))
    assert [e.kind for e in events] == ["user_prompt"]
    assert events[0].text == "go"
    assert events[0].uuid == "prt_1"
    assert events[0].parent_uuid == "msg_1"


def test_text_part_of_an_assistant_message_is_assistant_text():
    events = list(iter_events(_payload({"id": "prt_1", "type": "text", "text": "done"})))
    assert [e.kind for e in events] == ["assistant_text"]
    assert events[0].text == "done"


def test_empty_text_part_yields_nothing():
    assert list(iter_events(_payload({"id": "p", "type": "text", "text": "   "}))) == []


def test_reasoning_part_is_assistant_thinking():
    events = list(
        iter_events(_payload({"id": "prt_1", "type": "reasoning", "text": "hmm"}))
    )
    assert [e.kind for e in events] == ["assistant_thinking"]
    assert events[0].text == "hmm"


def test_bookkeeping_parts_yield_nothing():
    for ptype in ("step-start", "step-finish", "patch", "compaction"):
        payload = _payload({"id": f"prt_{ptype}", "type": ptype, "text": "x"})
        assert list(iter_events(payload)) == [], ptype


def test_unknown_part_type_yields_nothing():
    assert list(iter_events(_payload({"id": "p", "type": "brand-new"}))) == []


def test_tool_part_yields_one_tool_use():
    part = {
        "id": "prt_1",
        "type": "tool",
        "tool": "bash",
        "callID": "call_9",
        "state": {
            "status": "completed",
            "input": {"command": "uv run pytest -q"},
            "output": "185 passed",
            "time": {"start": 1753660845123, "end": 1753660846000},
        },
    }
    events = list(iter_events(_payload(part)))
    assert [e.kind for e in events] == ["tool_use"]
    e = events[0]
    assert e.tool_name == "bash"
    assert e.tool_use_id == "call_9"
    assert e.tool_input == {"command": "uv run pytest -q"}
    assert e.timestamp == "2025-07-28T00:00:45.123Z"


def test_failed_tool_part_also_yields_an_error_result():
    part = {
        "id": "prt_1",
        "type": "tool",
        "tool": "bash",
        "callID": "call_9",
        "state": {"status": "error", "input": {"command": "false"}, "error": "boom"},
    }
    events = list(iter_events(_payload(part)))
    assert [e.kind for e in events] == ["tool_use", "tool_result"]
    result = events[1]
    assert result.is_error is True
    assert result.text == "boom"
    assert result.tool_use_id == "call_9"
    assert result.uuid == "prt_1:error"


def test_failed_tool_part_falls_back_to_the_output(tmp_path: Path):
    part = {
        "id": "prt_1",
        "type": "tool",
        "tool": "bash",
        "state": {"status": "error", "output": "exit 1"},
    }
    events = list(iter_events(_payload(part)))
    assert events[1].text == "exit 1"


def test_tool_part_without_state_still_yields_a_tool_use():
    events = list(iter_events(_payload({"id": "p", "type": "tool", "tool": "read"})))
    assert [e.kind for e in events] == ["tool_use"]
    assert events[0].tool_input == {}


def test_part_falls_back_to_the_message_timestamp():
    events = list(iter_events(_payload({"id": "p", "type": "text", "text": "hi"})))
    assert events[0].timestamp == "2025-07-28T00:00:45.123Z"


def test_events_keep_export_order():
    payload = {
        "info": {"id": "ses_a", "directory": "/tmp/x"},
        "messages": [
            {
                "info": {"id": "msg_1", "role": "user", "time": {"created": 1}},
                "parts": [{"id": "p1", "type": "text", "text": "first"}],
            },
            {
                "info": {"id": "msg_2", "role": "assistant", "time": {"created": 2}},
                "parts": [
                    {"id": "p2", "type": "reasoning", "text": "think"},
                    {
                        "id": "p3",
                        "type": "tool",
                        "tool": "bash",
                        "state": {"status": "completed", "input": {"command": "ls"}},
                    },
                    {"id": "p4", "type": "text", "text": "last"},
                ],
            },
        ],
    }
    events = list(iter_events(payload))
    assert [e.uuid for e in events] == ["p1", "p2", "p3", "p4"]
    assert [e.kind for e in events] == [
        "user_prompt",
        "assistant_thinking",
        "tool_use",
        "assistant_text",
    ]


def test_malformed_messages_and_parts_are_skipped():
    payload = {"info": {"id": "ses_a"}, "messages": ["nope", {"parts": ["nope", None]}]}
    assert list(iter_events(payload)) == []


def test_payload_without_messages_yields_nothing():
    assert list(iter_events({"info": {"id": "ses_a"}})) == []


# ---------------------------------------------------------------------------
# Reasoning blob stripping
# ---------------------------------------------------------------------------


def test_reasoning_encrypted_content_is_stripped():
    part = {
        "id": "prt_1",
        "type": "reasoning",
        "text": "visible",
        "metadata": {"openai": {"reasoningEncryptedContent": "A" * 4096, "keep": 1}},
    }
    (event,) = list(iter_events(_payload(part)))
    assert "reasoningEncryptedContent" not in event.raw["metadata"]["openai"]
    assert event.raw["metadata"]["openai"]["keep"] == 1
    assert event.raw["text"] == "visible"


def test_stripping_leaves_the_original_part_untouched():
    part = {
        "type": "reasoning",
        "metadata": {"anthropic": {"reasoningEncryptedContent": "blob"}},
    }
    scrubbed = strip_reasoning_blob(part)
    assert scrubbed["metadata"]["anthropic"] == {}
    assert part["metadata"]["anthropic"]["reasoningEncryptedContent"] == "blob"


def test_stripping_tolerates_missing_metadata():
    assert strip_reasoning_blob({"type": "reasoning"}) == {"type": "reasoning"}
    assert strip_reasoning_blob({"metadata": "odd"}) == {"metadata": "odd"}


# ---------------------------------------------------------------------------
# Offloaded tool output
# ---------------------------------------------------------------------------


def test_tool_output_reads_an_offloaded_file(tmp_path: Path):
    out = tmp_path / "tool-output" / "prt_1"
    out.parent.mkdir()
    out.write_text("the full output")
    state = {"output": "truncated", "metadata": {"outputPath": str(out)}}
    assert tool_output(state) == "the full output"


def test_missing_tool_output_path_is_empty_not_an_error(tmp_path: Path):
    state = {"metadata": {"outputPath": str(tmp_path / "tool-output" / "gone")}}
    assert tool_output(state) == ""


def test_tool_output_uses_the_inline_output_by_default():
    assert tool_output({"status": "completed", "output": "inline"}) == "inline"
    assert tool_output({"metadata": {}}) == ""
    assert tool_output({"output": None}) == ""
    assert tool_output("not a dict") == ""


def test_failed_tool_with_a_missing_output_path_yields_empty_text(tmp_path: Path):
    part = {
        "id": "prt_1",
        "type": "tool",
        "tool": "bash",
        "state": {
            "status": "error",
            "metadata": {"outputPath": str(tmp_path / "gone")},
        },
    }
    events = list(iter_events(_payload(part)))
    assert [e.kind for e in events] == ["tool_use", "tool_result"]
    assert events[1].text == ""


# ---------------------------------------------------------------------------
# build_session
# ---------------------------------------------------------------------------


def test_build_session_maps_info_and_events(tmp_path: Path):
    payload = _payload(
        {"id": "prt_1", "type": "text", "text": "hello"},
        role="user",
        info={"id": "ses_08ed513b1ffeAv6xEe73sudEqi", "directory": "/home/u/src/app"},
    )
    session = build_session(payload, source_path=tmp_path / "o.db")
    assert session.session_id == "ses_08ed513b1ffeAv6xEe73sudEqi"
    assert session.cwd == "/home/u/src/app"
    assert session.path == tmp_path / "o.db"
    assert [e.kind for e in session.events] == ["user_prompt"]


def test_build_session_has_no_git_branch():
    """Opencode records no branch: workspace is empty and workspace_id is NULL."""
    session = build_session(_payload({"id": "p", "type": "text", "text": "hi"}))
    assert session.git_branch is None


def test_build_session_date_prefix_comes_from_epoch_ms():
    payload = _payload(
        {"id": "prt_1", "type": "text", "text": "hi", "time": {"start": 1753747245000}},
        role="user",
    )
    session = build_session(payload)
    assert session.started_at == "2025-07-29T00:00:45.000Z"
    assert session.date_prefix == "2025-07-29"


def test_build_session_date_prefix_falls_back_to_the_message_time():
    payload = _payload({"id": "prt_1", "type": "text", "text": "hi"}, role="user")
    assert build_session(payload).date_prefix == "2025-07-28"


def test_build_session_short_id_drops_the_ses_prefix():
    payload = _payload(info={"id": "ses_08ed513b1ffeAv6xEe73sudEqi"})
    assert build_session(payload).short_id == "08ed513b"


def test_build_session_of_an_empty_payload():
    session = build_session({})
    assert session.session_id == ""
    assert session.events == []
    assert session.cwd is None
    assert session.date_prefix == "0000-00-00"


# ---------------------------------------------------------------------------
# Export CLI contract
# ---------------------------------------------------------------------------


def _fake_run(monkeypatch, *, stdout="{}", stderr="", returncode=0, seen=None):
    """Stand in for the opencode binary.

    stdout is written into the sink the caller opened, not returned on the
    CompletedProcess: opencode exits without draining a pipe, so sagent
    spools its output to a file and the stub must honour the same contract.
    """

    def run(cmd, **kwargs):
        if seen is not None:
            seen.append((cmd, kwargs))
        sink = kwargs.get("stdout")
        if sink is not None and hasattr(sink, "write"):
            sink.write(stdout.encode("utf-8"))
        return subprocess.CompletedProcess(cmd, returncode, None, stderr)

    monkeypatch.setattr(opencode.subprocess, "run", run)


def test_resolve_binary_honours_opencode_bin(monkeypatch, tmp_path: Path):
    binary = tmp_path / "opencode"
    binary.touch()
    monkeypatch.setenv("OPENCODE_BIN", str(binary))
    assert resolve_binary() == binary


def test_resolve_binary_raises_when_absent(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("OPENCODE_BIN", raising=False)
    monkeypatch.setattr(opencode, "_BUNDLED_BINARY", tmp_path / "nope")
    monkeypatch.setattr(opencode.shutil, "which", lambda name: None)
    with pytest.raises(OpencodeBinaryError):
        resolve_binary()


def test_export_passes_pure_flag(monkeypatch, tmp_path: Path):
    binary = tmp_path / "opencode"
    binary.touch()
    monkeypatch.setenv("OPENCODE_BIN", str(binary))
    seen: list = []
    _fake_run(monkeypatch, stdout=json.dumps({"info": {"id": "ses_a"}}), seen=seen)
    payload = export_session("ses_a")
    assert payload == {"info": {"id": "ses_a"}}
    cmd = seen[0][0]
    assert cmd == [str(binary), "--pure", "export", "ses_a"]


def test_export_rejects_plugin_banner_on_stdout(monkeypatch, tmp_path: Path):
    binary = tmp_path / "opencode"
    binary.touch()
    monkeypatch.setenv("OPENCODE_BIN", str(binary))
    _fake_run(
        monkeypatch,
        stdout='[opencode-litellm] Discovered 15 models\n{"info": {}}',
    )
    with pytest.raises(OpencodeExportError):
        export_session("ses_a")


def test_export_raises_on_non_zero_exit(monkeypatch, tmp_path: Path):
    binary = tmp_path / "opencode"
    binary.touch()
    monkeypatch.setenv("OPENCODE_BIN", str(binary))
    _fake_run(monkeypatch, stdout="", stderr="no such session", returncode=1)
    with pytest.raises(OpencodeExportError):
        export_session("ses_a")


def test_export_raises_on_timeout(monkeypatch, tmp_path: Path):
    binary = tmp_path / "opencode"
    binary.touch()
    monkeypatch.setenv("OPENCODE_BIN", str(binary))

    def run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 1))

    monkeypatch.setattr(opencode.subprocess, "run", run)
    with pytest.raises(OpencodeExportError):
        export_session("ses_a", timeout=1)


def test_export_rejects_a_non_object_payload(monkeypatch, tmp_path: Path):
    binary = tmp_path / "opencode"
    binary.touch()
    monkeypatch.setenv("OPENCODE_BIN", str(binary))
    _fake_run(monkeypatch, stdout="[]")
    with pytest.raises(OpencodeExportError):
        export_session("ses_a")


def test_load_session_cites_the_database_as_its_source(monkeypatch, tmp_path: Path):
    binary = tmp_path / "opencode"
    binary.touch()
    monkeypatch.setenv("OPENCODE_BIN", str(binary))
    payload = _payload(
        {"id": "prt_1", "type": "text", "text": "hi"},
        role="user",
        info={"id": "ses_a", "directory": "/tmp/x"},
    )
    _fake_run(monkeypatch, stdout=json.dumps(payload))
    db = tmp_path / "o.db"
    session = load_session("ses_a", db_path=db)
    assert session.path == db
    assert session.session_id == "ses_a"
    assert session.git_branch is None
    assert [e.kind for e in session.events] == ["user_prompt"]


# ---------------------------------------------------------------------------
# Session.path must never be derived from the ledger key
# ---------------------------------------------------------------------------


def test_build_session_without_a_source_names_the_database_not_the_key(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    session = build_session(_payload(info={"id": "ses_a"}))
    # `Path("opencode://ses_a")` collapses to "opencode:/ses_a", which no
    # ledger lookup would ever match. The path must not be the key.
    assert "opencode:" not in str(session.path)
    assert session.path == tmp_path / "opencode" / "opencode.db"
    assert str(session.path) != opencode.ledger_key("ses_a")


def test_ledger_key_survives_a_path_round_trip_only_as_a_string():
    key = opencode.ledger_key("ses_a")
    assert key == "opencode://ses_a"
    assert str(Path(key)) != key  # the trap this fallback used to fall into


# ---------------------------------------------------------------------------
# A `file:` override is still opened read-only
# ---------------------------------------------------------------------------


def test_read_only_uri_adds_the_mode_to_a_plain_path():
    assert opencode._read_only_uri("/a/b.db") == "file:/a/b.db?mode=ro"


def test_read_only_uri_adds_the_mode_to_a_uri_without_one():
    assert opencode._read_only_uri("file:/a/b.db") == "file:/a/b.db?mode=ro"
    assert (
        opencode._read_only_uri("file:/a/b.db?immutable=1")
        == "file:/a/b.db?immutable=1&mode=ro"
    )


def test_read_only_uri_overrides_a_writable_mode():
    assert opencode._read_only_uri("file:/a/b.db?mode=rwc") == "file:/a/b.db?mode=ro"
    assert (
        opencode._read_only_uri("file:/a/b.db?mode=rw&cache=shared")
        == "file:/a/b.db?mode=ro&cache=shared"
    )


def test_a_writable_file_override_is_still_opened_read_only(tmp_path: Path):
    db = tmp_path / "opencode.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE session (id TEXT, directory TEXT, project_id TEXT, parent_id TEXT, time_updated INT)")
    conn.commit()
    conn.close()
    with opencode._connect(f"file:{db}?mode=rwc") as ro:
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("INSERT INTO session (id) VALUES ('x')")


def test_export_never_hands_the_binary_a_pipe(monkeypatch, tmp_path: Path):
    """opencode exits without draining stdout, so a pipe loses the tail.

    Measured on this host: a 618474-byte export arrived as 65490 bytes — one
    pipe buffer — and failed to parse mid-string. Every session larger than
    the buffer was unreadable, which is nearly all of the real ones.
    """
    binary = tmp_path / "opencode"
    binary.touch()
    monkeypatch.setenv("OPENCODE_BIN", str(binary))
    seen: list = []
    _fake_run(monkeypatch, stdout=json.dumps({"info": {"id": "ses_a"}}), seen=seen)
    export_session("ses_a")
    kwargs = seen[0][1]
    assert kwargs["stdout"] is not subprocess.PIPE
    assert hasattr(kwargs["stdout"], "fileno")


def test_export_reads_a_payload_far_larger_than_a_pipe_buffer(
    monkeypatch, tmp_path: Path
):
    binary = tmp_path / "opencode"
    binary.touch()
    monkeypatch.setenv("OPENCODE_BIN", str(binary))
    parts = [
        {"id": f"prt_{i}", "type": "text", "text": "x" * 400} for i in range(500)
    ]
    big = json.dumps(
        {
            "info": {"id": "ses_big"},
            "messages": [{"info": {"id": "msg_1", "role": "assistant"}, "parts": parts}],
        }
    )
    assert len(big) > 200_000  # several pipe buffers
    _fake_run(monkeypatch, stdout=big)
    payload = export_session("ses_big")
    assert len(payload["messages"][0]["parts"]) == 500


def test_export_decodes_utf8_beyond_ascii(monkeypatch, tmp_path: Path):
    binary = tmp_path / "opencode"
    binary.touch()
    monkeypatch.setenv("OPENCODE_BIN", str(binary))
    _fake_run(monkeypatch, stdout=json.dumps({"info": {"id": "ses_a"}, "note": "café → ✓"}))
    assert export_session("ses_a")["note"] == "café → ✓"
