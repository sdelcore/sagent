from __future__ import annotations

import json
import re
from pathlib import Path

from sagent.session_doc import (
    VerbatimCommands,
    VerbatimEntry,
    _tool_summary,
    _truncate,
    build_timeline,
    compose_session_md,
    extract_verbatim,
    patch_targets,
    render_verbatim_block,
    write_session_md,
)
from sagent.parser import Event, Session, load_session


def test_truncate_below_limit():
    assert _truncate("short", 200) == "short"


def test_truncate_collapses_whitespace():
    assert _truncate("a  b\n c", 200) == "a b c"


def test_truncate_above_limit():
    out = _truncate("x" * 500, 10)
    assert len(out) == 10
    assert out.endswith("…")


def test_tool_summary_edit():
    e = Event(
        kind="tool_use",
        uuid="",
        parent_uuid=None,
        timestamp=None,
        tool_name="Edit",
        tool_input={"file_path": "/a/b.py"},
    )
    assert _tool_summary(e) == "Edit(/a/b.py)"


def test_tool_summary_bash():
    e = Event(
        kind="tool_use",
        uuid="",
        parent_uuid=None,
        timestamp=None,
        tool_name="Bash",
        tool_input={"command": "echo hi"},
    )
    assert _tool_summary(e).startswith("Bash: echo hi")


def test_tool_summary_unknown_tool():
    e = Event(
        kind="tool_use",
        uuid="",
        parent_uuid=None,
        timestamp=None,
        tool_name="MysteryTool",
        tool_input={"foo": "bar"},
    )
    assert _tool_summary(e).startswith("MysteryTool(")


def _mini_session(tmp_path: Path) -> Session:
    p = tmp_path / "s.jsonl"
    records = [
        {
            "type": "user",
            "uuid": "u1",
            "parentUuid": None,
            "timestamp": "2026-04-22T10:00:00Z",
            "sessionId": "demo",
            "cwd": "/x",
            "gitBranch": "main",
            "message": {"role": "user", "content": [{"type": "text", "text": "build"}]},
        },
        {
            "type": "assistant",
            "uuid": "u2",
            "parentUuid": "u1",
            "timestamp": "2026-04-22T10:00:05Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "on it"},
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Write",
                        "input": {"file_path": "/x/a.py"},
                    },
                    {
                        "type": "tool_use",
                        "id": "t2",
                        "name": "Bash",
                        "input": {"command": "ls"},
                    },
                ],
            },
        },
    ]
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return load_session(p)


def test_build_timeline_contains_key_sections(tmp_path: Path):
    sess = _mini_session(tmp_path)
    out = build_timeline(sess)
    assert "# Timeline — `demo`" in out
    assert "**cwd:** `/x`" in out
    assert "**branch:** `main`" in out
    assert "## Tool usage" in out
    assert "`Write` × 1" in out
    assert "`Bash` × 1" in out
    assert "## Files written" in out
    assert "`/x/a.py`" in out
    assert "## Turn-by-turn" in out
    assert "### Turn 1" in out
    assert "**User:** build" in out
    assert "**Claude:** on it" in out


def test_compose_session_md_combines_sections(tmp_path: Path):
    sess = _mini_session(tmp_path)
    out = compose_session_md(
        sess,
        summary_md="# Summary\n\nUser asked to build something.",
        understanding_md="# Understanding\n\n## Decisions\n- Built it",
        project="src-x",
    )
    # front matter present
    assert out.startswith("---\n")
    assert 'type: "session"' in out
    assert 'project: "src-x"' in out
    assert 'source: "claude-code"' in out
    assert 'gist: "User asked to build something."' in out
    # body sections
    assert "# Session " in out
    assert "## Summary" in out
    assert "User asked to build something." in out
    assert "## Understanding" in out
    assert "Built it" in out
    # Timeline section is gone
    assert "## Timeline" not in out


# ---------------------------------------------------------------------------
# Verbatim commands block (D6)
# ---------------------------------------------------------------------------

# Longer than the 100-char cut the LLM-facing timeline applies. The verbatim
# block exists precisely so a command like this survives whole.
LONG_COMMAND = (
    "nix build .#nixosConfigurations.nightman.config.system.build.toplevel "
    "--print-build-logs --option substituters 'https://cache.nixos.org' "
    "--option trusted-public-keys 'cache.nixos.org-1:6NCHdD59X431o0gWyp+8=' "
    "2>&1 | tail -40"
)


def _tool_event(name: str, tool_input: dict, uuid: str = "t") -> Event:
    return Event(
        kind="tool_use",
        uuid=uuid,
        parent_uuid=None,
        timestamp="2026-04-22T10:00:00Z",
        tool_name=name,
        tool_input=tool_input,
    )


def _session_with(events: list[Event], session_id: str = "sess-0001") -> Session:
    return Session(session_id=session_id, path=Path("/tmp/s.jsonl"), events=events)


def test_long_command_is_truncated_by_tool_summary_but_not_by_verbatim():
    assert len(LONG_COMMAND) > 100
    e = _tool_event("Bash", {"command": LONG_COMMAND})
    assert LONG_COMMAND not in _tool_summary(e)
    assert extract_verbatim(_session_with([e])).entries == [LONG_COMMAND]


def test_extract_verbatim_claude_code_bash_and_write_targets():
    sess = _session_with(
        [
            _tool_event("Bash", {"command": "uv run pytest -q"}, "t1"),
            _tool_event("Write", {"file_path": "/x/a.py", "content": "..."}, "t2"),
            _tool_event("Edit", {"file_path": "/x/b.py"}, "t3"),
            _tool_event("NotebookEdit", {"notebook_path": "/x/n.ipynb"}, "t4"),
            _tool_event("Read", {"file_path": "/x/ignored.py"}, "t5"),
        ]
    )
    verbatim = extract_verbatim(sess)
    assert verbatim.entries == [
        "uv run pytest -q",
        "/x/a.py",
        "/x/b.py",
        "/x/n.ipynb",
    ]
    # A read is not a write, so its path stays out of the block.
    assert "/x/ignored.py" not in verbatim.entries


def test_extract_verbatim_opencode_lowercase_tool_names():
    """Opencode names its tools `bash` / `write` / `edit`, all lower case."""
    sess = _session_with(
        [
            _tool_event("bash", {"command": LONG_COMMAND}, "prt_1"),
            _tool_event("write", {"filePath": "/srv/app/main.py"}, "prt_2"),
            _tool_event("edit", {"filePath": "/srv/app/util.py"}, "prt_3"),
            _tool_event("apply_patch", {"path": "/srv/app/patched.py"}, "prt_4"),
        ]
    )
    assert extract_verbatim(sess).entries == [
        LONG_COMMAND,
        "/srv/app/main.py",
        "/srv/app/util.py",
        "/srv/app/patched.py",
    ]


def test_extract_verbatim_ignores_non_tool_events_and_bad_input():
    sess = _session_with(
        [
            Event("user_prompt", "u1", None, None, text="run the build"),
            Event("assistant_text", "a1", None, None, text="ok"),
            _tool_event("Bash", {"command": "   "}, "t1"),
            Event(
                kind="tool_use",
                uuid="t2",
                parent_uuid=None,
                timestamp=None,
                tool_name="Bash",
                tool_input=None,
            ),
            _tool_event("Bash", {"command": "make"}, "t3"),
        ]
    )
    verbatim = extract_verbatim(sess)
    assert verbatim.entries == ["make"]
    assert verbatim.raw_count == 1


def test_extract_verbatim_dedups_exact_repeats_keeping_first_seen_order():
    sess = _session_with(
        [
            _tool_event("Bash", {"command": "git status"}, "t1"),
            _tool_event("Bash", {"command": "uv run pytest -q"}, "t2"),
            _tool_event("Bash", {"command": "git status"}, "t3"),
            _tool_event("Write", {"file_path": "/x/a.py"}, "t4"),
            _tool_event("Edit", {"file_path": "/x/a.py"}, "t5"),
            _tool_event("Bash", {"command": "uv run pytest -q"}, "t6"),
        ]
    )
    verbatim = extract_verbatim(sess)
    assert verbatim.entries == ["git status", "uv run pytest -q", "/x/a.py"]
    assert verbatim.raw_count == 6


def test_render_verbatim_block_states_dedup_provenance():
    sess = _session_with(
        [
            _tool_event("Bash", {"command": "ls"}, "t1"),
            _tool_event("Bash", {"command": "ls"}, "t2"),
            _tool_event("Bash", {"command": "pwd"}, "t3"),
        ]
    )
    block = render_verbatim_block(extract_verbatim(sess))
    assert block.startswith("## Commands (verbatim)")
    # One fence per entry (F2): a shared fence cannot delimit commands that
    # contain their own newlines.
    assert "```\nls\n```" in block
    assert "```\npwd\n```" in block
    assert "_3 bash calls deduped to 2 commands_" in block
    assert "omitted" not in block


def test_render_verbatim_block_caps_budget_and_counts_omitted():
    entries = [f"echo {'x' * 20} {i}" for i in range(10)]
    sess = _session_with(
        [_tool_event("Bash", {"command": c}, f"t{i}") for i, c in enumerate(entries)]
    )
    block = render_verbatim_block(extract_verbatim(sess), budget=80)
    assert entries[0] in block
    assert entries[-1] not in block
    assert "_10 bash calls deduped to 10 commands - 8 omitted over budget_" in block


def test_render_verbatim_block_keeps_first_command_even_over_budget():
    sess = _session_with([_tool_event("Bash", {"command": LONG_COMMAND})])
    block = render_verbatim_block(extract_verbatim(sess), budget=10)
    assert LONG_COMMAND in block
    assert "_1 bash call deduped to 1 command_" in block


def test_render_verbatim_block_widens_fence_around_backticks():
    cmd = "printf '```json\\n{}\\n```' > /x/readme.md"
    sess = _session_with([_tool_event("Bash", {"command": cmd})])
    block = render_verbatim_block(extract_verbatim(sess))
    assert "````\n" + cmd + "\n````" in block


def test_render_verbatim_block_empty_when_nothing_ran():
    sess = _session_with([Event("user_prompt", "u1", None, None, text="hi")])
    assert render_verbatim_block(extract_verbatim(sess)) == ""


def test_compose_session_md_appends_full_command(tmp_path: Path):
    sess = _mini_session(tmp_path)
    sess.events.append(_tool_event("Bash", {"command": LONG_COMMAND}, "t9"))
    out = compose_session_md(
        sess,
        summary_md="# Summary\n\nBuilt it.",
        understanding_md="# Understanding\n\n- ok",
        project="src-x",
    )
    assert "## Commands (verbatim)" in out
    assert LONG_COMMAND in out
    # The block comes after the LLM sections, never in front of them.
    assert out.index("## Understanding") < out.index("## Commands (verbatim)")


def test_compose_session_md_omits_block_without_commands(tmp_path: Path):
    sess = _session_with([Event("user_prompt", "u1", None, None, text="just talking")])
    out = compose_session_md(
        sess,
        summary_md="# Summary\n\nTalked.",
        understanding_md="# Understanding\n\n- nothing ran",
        project="src-x",
    )
    assert "## Commands (verbatim)" not in out


def test_compose_session_md_budget_is_configurable(tmp_path: Path):
    sess = _mini_session(tmp_path)
    sess.events.append(_tool_event("Bash", {"command": LONG_COMMAND}, "t9"))
    out = compose_session_md(
        sess,
        summary_md="# Summary\n\nBuilt it.",
        understanding_md="# Understanding\n\n- ok",
        project="src-x",
        verbatim_budget=5,
    )
    # /x/a.py is the first entry, so the long command falls off the budget.
    assert "omitted over budget" in out
    assert LONG_COMMAND not in out


# ---------------------------------------------------------------------------
# Harness front matter (D5)
# ---------------------------------------------------------------------------


def test_compose_session_md_defaults_harness_to_claude_code(tmp_path: Path):
    out = compose_session_md(
        _mini_session(tmp_path),
        summary_md="# Summary\n\nx",
        understanding_md="",
        project="src-x",
    )
    assert 'harness: "claude-code"' in out


def test_compose_session_md_records_opencode_harness(tmp_path: Path):
    out = compose_session_md(
        _mini_session(tmp_path),
        summary_md="# Summary\n\nx",
        understanding_md="",
        project="src-x",
        source="opencode",
        harness="opencode",
    )
    assert 'harness: "opencode"' in out
    assert 'source: "opencode"' in out


# ---------------------------------------------------------------------------
# apply_patch targets live inside the patch document (opencode's real shape)
# ---------------------------------------------------------------------------

# Verbatim copy of the shape the live opencode database stores: the only
# input key is `patchText`, and the targets are named in the patch headers.
OPENCODE_PATCH = """*** Begin Patch
*** Update File: nix/modules/software/localllm/llama-swap.nix
@@
-      extra = "--cpu-moe"
+      extra = "--cpu-moe --no-mmap"
*** Delete File: home/modules/pi/local-model.nix
*** Add File: home/modules/pi/local-mo.nix
+{ ... }
*** End Patch
"""


def test_patch_targets_reads_add_update_and_delete_headers():
    assert patch_targets({"patchText": OPENCODE_PATCH}) == [
        "nix/modules/software/localllm/llama-swap.nix",
        "home/modules/pi/local-model.nix",
        "home/modules/pi/local-mo.nix",
    ]


def test_patch_targets_of_an_input_without_a_patch():
    assert patch_targets({"file_path": "/a/b.py"}) == []
    assert patch_targets({"patchText": ""}) == []
    assert patch_targets({"patchText": 17}) == []


def test_extract_verbatim_finds_opencode_apply_patch_targets():
    # opencode names no path key at all, so without patch parsing every
    # opencode file write is missing from the block.
    sess = _session_with(
        [_tool_event("apply_patch", {"patchText": OPENCODE_PATCH})]
    )
    assert extract_verbatim(sess).entries == [
        "nix/modules/software/localllm/llama-swap.nix",
        "home/modules/pi/local-model.nix",
        "home/modules/pi/local-mo.nix",
    ]


def test_extract_verbatim_dedups_a_path_named_twice_by_one_patch_tool():
    sess = _session_with(
        [
            _tool_event(
                "apply_patch",
                {
                    "filePath": "/a/b.py",
                    "patchText": "*** Update File: /a/b.py\n",
                },
            )
        ]
    )
    v = extract_verbatim(sess)
    assert v.entries == ["/a/b.py"]
    assert v.raw_count == 2


def test_extract_verbatim_accepts_a_bare_patch_tool_name():
    sess = _session_with([_tool_event("patch", {"patchText": OPENCODE_PATCH})])
    assert "home/modules/pi/local-mo.nix" in extract_verbatim(sess).entries


def test_compose_session_md_lists_a_patched_file(tmp_path: Path):
    sess = _mini_session(tmp_path)
    sess.events.append(
        _tool_event("apply_patch", {"patchText": OPENCODE_PATCH}, "t9")
    )
    out = compose_session_md(
        sess,
        summary_md="# Summary\n\nx",
        understanding_md="",
        project="src-x",
        harness="opencode",
    )
    assert "## Commands (verbatim)" in out
    assert "nix/modules/software/localllm/llama-swap.nix" in out


# ---------------------------------------------------------------------------

def _block_for(commands: list[str], **kw) -> str:
    sess = _session_with(
        [_tool_event("Bash", {"command": c}, f"t{i}") for i, c in enumerate(commands)]
    )
    return render_verbatim_block(extract_verbatim(sess), **kw)


# ---------------------------------------------------------------------------

def test_verbatim_budget_zero_drops_the_block_entirely():
    assert _block_for(["git status"], budget=0) == ""
    assert _block_for(["git status"], budget=-1) == ""


def test_compose_session_md_budget_zero_keeps_shell_history_out(tmp_path: Path):
    command = "ssh root@10.0.0.13 uptime"
    sess = _mini_session(tmp_path)
    sess.events.append(_tool_event("Bash", {"command": command}, "t9"))
    out = compose_session_md(
        sess,
        summary_md="# Summary\n\nx",
        understanding_md="",
        project="src-x",
        verbatim_budget=0,
    )
    assert "## Commands (verbatim)" not in out
    assert command not in out
    # The LLM sections are untouched by a zero budget.
    assert "## Summary" in out


# ---------------------------------------------------------------------------
# Rendering: one fence per entry, paths in their own subsection (F2)
# ---------------------------------------------------------------------------

HEREDOC_COMMAND = (
    "cat <<'EOF' > /tmp/notes.md\n"
    "# notes\n"
    "\n"
    "```sh\n"
    "uv run pytest -q\n"
    "```\n"
    "EOF"
)

_FENCE_LINE_RE = re.compile(r"^(`{3,})$")


def _fenced_entries(block: str) -> list[str]:
    """Recover one entry per fenced block, the way a reader has to.

    Deliberately dumb: a bare backtick run opens an entry, and only the same
    run closes it. That is the contract F2 relies on, so the test parses the
    text instead of trusting the renderer's own count.
    """
    entries: list[str] = []
    body: list[str] | None = None
    delim = ""
    for line in block.split("\n"):
        if body is None:
            m = _FENCE_LINE_RE.match(line)
            if m:
                delim = m.group(1)
                body = []
            continue
        if line == delim:
            entries.append("\n".join(body))
            body = None
            continue
        body.append(line)
    assert body is None, "an entry fence was never closed"
    return entries


def _shown_of(block: str, heading: str) -> tuple[int, int]:
    m = re.search(rf"### {heading} \((\d+) of (\d+) shown\)", block)
    assert m, f"no '{heading}' subsection heading in block"
    return int(m.group(1)), int(m.group(2))


def test_entry_count_is_recoverable_from_a_block_holding_a_heredoc():
    commands = ["git status", HEREDOC_COMMAND, "uv run pytest -q"]
    block = _block_for(commands)
    recovered = _fenced_entries(block)
    assert recovered == commands
    shown, total = _shown_of(block, "Shell commands")
    assert shown == len(recovered) == 3
    assert total == 3


def test_shown_count_matches_the_entries_actually_present_under_budget():
    commands = [f"echo {'x' * 20} {i}" for i in range(10)]
    block = _block_for(commands, budget=80)
    shown, total = _shown_of(block, "Shell commands")
    assert shown == len(_fenced_entries(block))
    assert total == 10
    assert f"{total - shown} omitted over budget" in block


def test_a_heredoc_entry_is_delimited_by_a_widened_fence():
    block = _block_for([HEREDOC_COMMAND])
    # The entry contains a ``` line, so its own fence must be longer.
    assert "````\n" + HEREDOC_COMMAND + "\n````" in block


def test_written_paths_live_in_their_own_subsection():
    sess = _session_with(
        [
            _tool_event("Bash", {"command": "git status"}, "t1"),
            _tool_event("Write", {"file_path": "/x/a.py"}, "t2"),
            _tool_event("Edit", {"file_path": "/x/b.py"}, "t3"),
        ]
    )
    block = render_verbatim_block(extract_verbatim(sess))
    assert block.index("### Shell commands") < block.index("### Files written")
    # A path is never rendered as a fenced line, which would read as runnable.
    assert _fenced_entries(block) == ["git status"]
    assert "- `/x/a.py`" in block
    assert "- `/x/b.py`" in block
    assert _shown_of(block, "Files written") == (2, 2)


def test_provenance_reports_commands_and_paths_as_two_populations():
    sess = _session_with(
        [
            _tool_event("Bash", {"command": "git status"}, "t1"),
            _tool_event("Bash", {"command": "git status"}, "t2"),
            _tool_event("Write", {"file_path": "/x/a.py"}, "t3"),
            _tool_event("Edit", {"file_path": "/x/a.py"}, "t4"),
            _tool_event("Edit", {"file_path": "/x/b.py"}, "t5"),
        ]
    )
    block = render_verbatim_block(extract_verbatim(sess))
    assert (
        "_2 bash calls deduped to 1 command - 3 writes deduped to 2 paths_" in block
    )


def test_a_block_of_paths_alone_has_no_shell_subsection():
    sess = _session_with([_tool_event("Write", {"file_path": "/x/a.py"}, "t1")])
    block = render_verbatim_block(extract_verbatim(sess))
    assert "### Shell commands" not in block
    assert _fenced_entries(block) == []
    assert "- `/x/a.py`" in block


# ---------------------------------------------------------------------------
# Per-call working directory (F3)
# ---------------------------------------------------------------------------


def _opencode_session(events: list[Event], cwd: str = "/home/x/proj") -> Session:
    return Session(
        session_id="ses_08ed513b",
        path=Path("/tmp/s.jsonl"),
        events=events,
        cwd=cwd,
    )


def test_same_command_in_two_workdirs_stays_two_visible_entries():
    sess = _opencode_session(
        [
            _tool_event(
                "bash", {"command": "git log --oneline -3", "workdir": "/srv/a"}, "p1"
            ),
            _tool_event(
                "bash", {"command": "git log --oneline -3", "workdir": "/srv/b"}, "p2"
            ),
        ]
    )
    verbatim = extract_verbatim(sess)
    assert [(i.text, i.workdir) for i in verbatim.items] == [
        ("git log --oneline -3", "/srv/a"),
        ("git log --oneline -3", "/srv/b"),
    ]
    block = render_verbatim_block(verbatim)
    assert _fenced_entries(block) == ["git log --oneline -3"] * 2
    assert "_in `/srv/a`_" in block
    assert "_in `/srv/b`_" in block
    assert _shown_of(block, "Shell commands") == (2, 2)


def test_a_workdir_equal_to_the_session_cwd_is_not_printed():
    sess = _opencode_session(
        [
            _tool_event("bash", {"command": "git status", "workdir": "/home/x/proj"}),
        ]
    )
    verbatim = extract_verbatim(sess)
    assert verbatim.items[0].workdir == ""
    assert "_in `" not in render_verbatim_block(verbatim)


def test_a_trailing_slash_does_not_make_the_session_cwd_look_foreign():
    sess = _opencode_session(
        [_tool_event("bash", {"command": "git status", "workdir": "/home/x/proj/"})]
    )
    assert extract_verbatim(sess).items[0].workdir == ""


def test_opencode_also_accepts_a_cwd_key():
    sess = _opencode_session(
        [_tool_event("bash", {"command": "git status", "cwd": "/srv/other"})]
    )
    assert extract_verbatim(sess).items[0].workdir == "/srv/other"


def test_the_claude_code_path_is_unchanged_by_the_workdir_rule():
    """Claude Code sends no per-call directory, so no entry gains a cwd line."""
    sess = Session(
        session_id="1a2b3c4d-0000",
        path=Path("/tmp/s.jsonl"),
        events=[
            _tool_event("Bash", {"command": "git status"}, "t1"),
            _tool_event("Bash", {"command": "uv run pytest -q"}, "t2"),
            _tool_event("Write", {"file_path": "/x/a.py"}, "t3"),
        ],
        cwd="/home/x/proj",
    )
    verbatim = extract_verbatim(sess)
    assert all(i.workdir == "" for i in verbatim.items)
    block = render_verbatim_block(verbatim)
    assert "_in `" not in block
    assert _fenced_entries(block) == ["git status", "uv run pytest -q"]


def test_two_identical_commands_in_one_workdir_still_dedup():
    sess = _opencode_session(
        [
            _tool_event("bash", {"command": "make", "workdir": "/srv/a"}, "p1"),
            _tool_event("bash", {"command": "make", "workdir": "/srv/a"}, "p2"),
        ]
    )
    verbatim = extract_verbatim(sess)
    assert len(verbatim.items) == 1
    assert verbatim.command_calls == 2


# ---------------------------------------------------------------------------
# Byte-exactness (F4)
# ---------------------------------------------------------------------------


def test_a_trailing_newline_survives_extraction_and_rendering():
    """A heredoc terminator needs its newline, so extraction keeps it."""
    command = "cat <<'EOF' > /tmp/x\nhello\nEOF\n"
    sess = _session_with([_tool_event("Bash", {"command": command})])
    verbatim = extract_verbatim(sess)
    assert verbatim.items[0].text == command
    assert _fenced_entries(render_verbatim_block(verbatim)) == [command]


def test_leading_whitespace_survives_extraction():
    command = "  git status\n"
    sess = _session_with([_tool_event("Bash", {"command": command})])
    assert extract_verbatim(sess).items[0].text == command


def test_whitespace_only_commands_are_still_dropped():
    """`.strip()` decides emptiness only — it never rewrites a kept entry."""
    sess = _session_with(
        [
            _tool_event("Bash", {"command": "\n  \t "}, "t1"),
            _tool_event("Bash", {"command": " make \n"}, "t2"),
        ]
    )
    verbatim = extract_verbatim(sess)
    assert [i.text for i in verbatim.items] == [" make \n"]
    assert verbatim.command_calls == 1


def test_two_commands_differing_only_in_whitespace_stay_two_entries():
    sess = _session_with(
        [
            _tool_event("Bash", {"command": "make"}, "t1"),
            _tool_event("Bash", {"command": "make\n"}, "t2"),
        ]
    )
    assert [i.text for i in extract_verbatim(sess).items] == ["make", "make\n"]
