from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from sagent import pipeline
from sagent.pipeline import DigestConfig, digest_session
from sagent.rate import SagentRateLimitError
from sagent.state import DigestLedger


# ---------------------------------------------------------------------------
# Helpers: write a tiny JSONL session under a project-shaped directory.
# ---------------------------------------------------------------------------


def _write_session(
    project_root: Path,
    session_id: str = "abcd1234-aaaa-bbbb-cccc-deadbeef0001",
    *,
    user_prompts: list[str] | None = None,
    extra_events: list[dict] | None = None,
    cwd: str = "/x/y",
    project_dir_name: str = "-home-sdelcore-src-demo",
) -> Path:
    """Write a JSONL session that load_session() can parse.

    Returns the path. The parent directory is named `project_dir_name`
    so that pipeline.project_dir_for() can derive a clean project name.
    """
    proj = project_root / project_dir_name
    proj.mkdir(parents=True, exist_ok=True)
    p = proj / f"{session_id}.jsonl"

    if user_prompts is None:
        user_prompts = ["please build it"]
    records: list[dict] = []
    for i, text in enumerate(user_prompts):
        records.append(
            {
                "type": "user",
                "uuid": f"u{i}",
                "parentUuid": None,
                "timestamp": f"2026-04-22T10:0{i}:00Z",
                "sessionId": session_id,
                "cwd": cwd,
                "gitBranch": "main",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": text}],
                },
            }
        )
    if extra_events:
        records.extend(extra_events)
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return p


def _config(out_root: Path, **overrides) -> DigestConfig:
    base = dict(
        out_root=out_root,
        verbose=False,
        skip_rollup=True,
    )
    base.update(overrides)
    return DigestConfig(**base)


@pytest.fixture
def fake_llm(monkeypatch):
    """Replace run_understanding with a stub that records calls."""
    calls: list[dict] = []

    def stub(session, model="claude-haiku-4-5", **kw):
        calls.append({"session_id": session.session_id, "kwargs": kw, "model": model})
        return ("# Summary\n\nA concise summary.\n", "# Understanding\n\n## Decisions\n- it\n")

    monkeypatch.setattr(pipeline, "run_understanding", stub)
    return calls


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_cold_start_writes_session_md_and_returns_full(tmp_path: Path, fake_llm):
    src = _write_session(tmp_path / "src")
    out = tmp_path / "out"
    outcome = digest_session(src, _config(out))

    assert outcome.status == "digested"
    assert outcome.mode == "full"
    assert outcome.out_path is not None
    assert outcome.out_path.exists()
    assert outcome.out_path.read_text().startswith("---\n")
    assert len(fake_llm) == 1


def test_skip_when_already_digested(tmp_path: Path, fake_llm):
    src = _write_session(tmp_path / "src")
    out = tmp_path / "out"
    state_path = tmp_path / "state.json"
    ledger = DigestLedger(state_path)

    first = digest_session(src, _config(out), ledger=ledger)
    assert first.status == "digested"

    second = digest_session(src, _config(out), ledger=ledger)
    assert second.status == "skipped"
    assert second.reason and "already digested" in second.reason
    # LLM only called once
    assert len(fake_llm) == 1


def test_drop_low_prompts(tmp_path: Path, fake_llm):
    src = _write_session(tmp_path / "src", user_prompts=[])
    out = tmp_path / "out"
    outcome = digest_session(src, _config(out, min_prompts=1))

    assert outcome.status == "dropped"
    assert outcome.reason and "user prompts" in outcome.reason
    assert fake_llm == []  # never called


def test_drop_self_generated(tmp_path: Path, fake_llm):
    src = _write_session(
        tmp_path / "src",
        user_prompts=["Session `xyz` (cwd: `/foo`, branch: `main`)\n\nTranscript:"],
    )
    out = tmp_path / "out"
    outcome = digest_session(src, _config(out))

    assert outcome.status == "dropped"
    assert outcome.reason == "sagent-self-generated"
    assert fake_llm == []


def test_no_llm_writes_placeholder(tmp_path: Path, fake_llm):
    src = _write_session(tmp_path / "src")
    out = tmp_path / "out"
    outcome = digest_session(src, _config(out, no_llm=True))

    assert outcome.status == "digested"
    assert outcome.mode == "no_llm"
    assert outcome.out_path is not None
    body = outcome.out_path.read_text()
    assert "LLM digest skipped" in body
    assert fake_llm == []  # LLM not invoked


def test_incremental_when_session_grew(tmp_path: Path, fake_llm):
    src = _write_session(tmp_path / "src", user_prompts=["first prompt"])
    out = tmp_path / "out"
    ledger = DigestLedger(tmp_path / "state.json")

    digest_session(src, _config(out), ledger=ledger)
    # Append more events so the file size grows past last_digested_size.
    extra = {
        "type": "user",
        "uuid": "u9",
        "parentUuid": None,
        "timestamp": "2026-04-22T10:09:00Z",
        "sessionId": "abcd1234-aaaa-bbbb-cccc-deadbeef0001",
        "cwd": "/x/y",
        "gitBranch": "main",
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": "second prompt"}],
        },
    }
    with src.open("a") as f:
        f.write(json.dumps(extra) + "\n")

    fake_llm.clear()
    outcome = digest_session(src, _config(out), ledger=ledger)

    assert outcome.status == "digested"
    assert outcome.mode == "incremental"
    assert outcome.new_events is not None and outcome.new_events >= 1
    # The LLM was called with prior_summary populated.
    assert fake_llm[0]["kwargs"].get("prior_summary", "").strip() != ""


def test_force_full_overrides_state(tmp_path: Path, fake_llm):
    src = _write_session(tmp_path / "src")
    out = tmp_path / "out"
    ledger = DigestLedger(tmp_path / "state.json")
    digest_session(src, _config(out), ledger=ledger)
    fake_llm.clear()

    outcome = digest_session(src, _config(out, force_full=True), ledger=ledger)
    assert outcome.status == "digested"
    assert outcome.mode == "full"
    # Cold rebuild — no prior_summary passed.
    assert fake_llm[0]["kwargs"].get("prior_summary", "") == ""


def test_skip_rollup_does_not_call_roll_up_project(tmp_path: Path, fake_llm, monkeypatch):
    called: list[bool] = []

    def boom(*a, **kw):
        called.append(True)

    monkeypatch.setattr(pipeline, "roll_up_project", boom)
    monkeypatch.setattr(pipeline, "update_index", lambda *_a, **_k: None)

    src = _write_session(tmp_path / "src")
    out = tmp_path / "out"
    digest_session(src, _config(out, skip_rollup=True))
    assert called == []


def test_rollup_runs_when_not_skipped(tmp_path: Path, fake_llm, monkeypatch):
    called: list[Path] = []

    def fake_rollup(project_dir, **kw):
        called.append(project_dir)

    monkeypatch.setattr(pipeline, "roll_up_project", fake_rollup)
    monkeypatch.setattr(pipeline, "update_index", lambda *_a, **_k: None)

    src = _write_session(tmp_path / "src")
    out = tmp_path / "out"
    digest_session(src, _config(out, skip_rollup=False))
    assert len(called) == 1
    # Project dir was derived under out/.
    assert called[0].parent == out


def test_rate_limit_re_raises(tmp_path: Path, monkeypatch):
    def boom(*a, **kw):
        raise SagentRateLimitError("hit it")

    monkeypatch.setattr(pipeline, "run_understanding", boom)
    src = _write_session(tmp_path / "src")
    out = tmp_path / "out"
    with pytest.raises(SagentRateLimitError):
        digest_session(src, _config(out))


def test_understanding_failure_returns_dropped(tmp_path: Path, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("model exploded")

    monkeypatch.setattr(pipeline, "run_understanding", boom)
    src = _write_session(tmp_path / "src")
    out = tmp_path / "out"
    outcome = digest_session(src, _config(out))
    assert outcome.status == "dropped"
    assert outcome.reason and "understanding failed" in outcome.reason


def test_missing_source_returns_dropped(tmp_path: Path):
    out = tmp_path / "out"
    outcome = digest_session(tmp_path / "nope.jsonl", _config(out))
    assert outcome.status == "dropped"


# ---------------------------------------------------------------------------
# Verbatim commands (D6) and project keying across harnesses (D4, D5)
# ---------------------------------------------------------------------------

LONG_COMMAND = (
    "nix build .#nixosConfigurations.nightman.config.system.build.toplevel "
    "--print-build-logs --option substituters 'https://cache.nixos.org' "
    "--option trusted-public-keys 'cache.nixos.org-1:6NCHdD59X431o0gWyp+8=' "
    "2>&1 | tail -40"
)

NIXOS_CWD = str(Path.home() / "src" / "infra" / "nixos")
NIXOS_CLAUDE_DIR = NIXOS_CWD.replace("/", "-")


def _bash_record(command: str, uuid: str = "a1") -> dict:
    return {
        "type": "assistant",
        "uuid": uuid,
        "parentUuid": "u0",
        "timestamp": "2026-04-22T10:05:00Z",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": f"t-{uuid}",
                    "name": "Bash",
                    "input": {"command": command},
                }
            ],
        },
    }


def test_no_llm_path_still_writes_verbatim_commands(tmp_path: Path, fake_llm):
    src = _write_session(
        tmp_path / "src",
        extra_events=[_bash_record(LONG_COMMAND)],
    )
    out = tmp_path / "out"
    outcome = digest_session(src, _config(out, no_llm=True))

    assert outcome.mode == "no_llm"
    assert outcome.out_path is not None
    body = outcome.out_path.read_text()
    assert "LLM digest skipped" in body
    # The deterministic block does not depend on the LLM running.
    assert "## Commands (verbatim)" in body
    assert LONG_COMMAND in body
    assert fake_llm == []


def test_session_md_records_the_claude_code_harness(tmp_path: Path, fake_llm):
    src = _write_session(tmp_path / "src")
    outcome = digest_session(src, _config(tmp_path / "out"))
    assert outcome.harness == "claude-code"
    assert 'harness: "claude-code"' in outcome.out_path.read_text()


def test_opencode_directory_maps_to_the_claude_code_project_key():
    """D4: one working tree gets one project key, whichever harness ran."""
    from sagent import opencode

    claude_key = pipeline.clean_project_name(NIXOS_CLAUDE_DIR)
    assert claude_key == "src-infra-nixos"
    assert opencode.project_key(NIXOS_CWD) == claude_key
    assert pipeline.project_name_for_cwd(NIXOS_CWD) == claude_key
    # A trailing slash is the same directory, so it is the same key.
    assert opencode.project_key(NIXOS_CWD + "/") == claude_key


def test_both_harnesses_write_into_one_project_dir(tmp_path: Path, fake_llm, monkeypatch):
    """The same cwd from opencode and Claude Code shares one project.md."""
    from sagent import opencode
    from sagent.parser import Event, Session

    out = tmp_path / "out"
    claude_src = _write_session(
        tmp_path / "src",
        cwd=NIXOS_CWD,
        project_dir_name=NIXOS_CLAUDE_DIR,
        extra_events=[_bash_record("nixos-rebuild switch --flake .#nightman")],
    )
    claude_outcome = digest_session(claude_src, _config(out))

    oc_session = Session(
        session_id="ses_08ed513b1ffeAv6xEe73sudEqi",
        path=tmp_path / "opencode.db",
        events=[
            Event(
                "user_prompt",
                "prt_0",
                None,
                "2026-04-23T09:00:00Z",
                text="rebuild the desktop",
            ),
            Event(
                kind="tool_use",
                uuid="prt_1",
                parent_uuid="msg_1",
                timestamp="2026-04-23T09:00:10Z",
                tool_name="bash",
                tool_input={"command": LONG_COMMAND},
            ),
        ],
        cwd=NIXOS_CWD,
    )
    monkeypatch.setattr(
        opencode,
        "list_sessions",
        lambda _db: [
            opencode.SessionRow(
                session_id=oc_session.session_id,
                directory=NIXOS_CWD,
                project_id="global",
                time_updated=1,
            )
        ],
    )
    monkeypatch.setattr(opencode, "load_session", lambda *_a, **_k: oc_session)

    oc_outcome = pipeline.digest_opencode_session(
        oc_session.session_id,
        _config(out),
        db_path=tmp_path / "opencode.db",
        directory=NIXOS_CWD,
        size=4096,
    )

    assert oc_outcome.status == "digested"
    assert oc_outcome.harness == "opencode"
    assert claude_outcome.out_path.parent == oc_outcome.out_path.parent
    assert claude_outcome.out_path.parent.parent == out / "src-infra-nixos"

    body = oc_outcome.out_path.read_text()
    assert 'harness: "opencode"' in body
    # An opencode `bash` part is a shell command like any other.
    assert LONG_COMMAND in body


# ---------------------------------------------------------------------------
# Roll-up refusal (D13, F8)
#
# The reviewer's scenario: the model answers conversationally, `project.md`
# is (correctly) not overwritten — and the session claim was committed
# anyway, so the next pass skipped the session as "already digested" and the
# refused content never reached project.md again.
# ---------------------------------------------------------------------------

# The exact reply that overwrote a real project.md and zeroed its counts.
CONVERSATIONAL_REPLY = (
    "The session digest you've provided is incomplete - it shows the session "
    "just started (5 events, 1 tool call) and the Explore agent results "
    "haven't arrived yet.\n"
    "\n"
    "Which would you prefer?"
)

VALID_ROLLUP_OUTPUT = (
    "DESCRIPTION: The demo service.\n"
    "TAGLINE: moving the deploy target.\n"
    "\n"
    "# demo\n"
    "\n"
    "## Current state\n"
    "Deploy target moved to Azure Container Apps.\n"
    "\n"
    "## Decisions\n"
    "- **deploy to Azure Container Apps** (locked in 2026-07-28)\n"
)

PRIOR_PROJECT_MD = (
    "---\n"
    'type: "project"\n'
    'project: "demo"\n'
    'description: "The demo service."\n'
    'tagline: "running on proxmox"\n'
    "session_count: 12\n"
    "decisions: 3\n"
    "open_threads: 2\n"
    'last_updated: "2026-07-20T10:00:00Z"\n'
    "---\n"
    "# demo\n"
    "\n"
    "## Current state\n"
    "Deployed from proxmox.\n"
    "\n"
    "## Decisions\n"
    "- **deploy from proxmox** (locked in 2026-03-01)\n"
    "- **key on the working directory** (locked in 2026-04-02)\n"
    "- **one digest per host** (locked in 2026-05-05)\n"
    "\n"
    "## Open threads\n"
    "- move off proxmox (raised 2026-07-19)\n"
    "- audit the token budget (raised 2026-07-20)\n"
)

PROJECT_DIR_NAME = "-home-sdelcore-src-demo"


def _project_dir(out_root: Path) -> Path:
    return out_root / pipeline.clean_project_name(PROJECT_DIR_NAME)


@pytest.fixture
def rollup_reply(monkeypatch, tmp_path: Path):
    """Drive the real roll-up with a scripted model reply.

    The whole chain runs — `roll_up_project`, the output guard, the D13
    handler in `_digest` — so only the model itself is faked. Returns a
    one-key dict; assign `reply` to change what the model says next, and
    read `calls` for how many roll-up calls were made.
    """
    import sagent.rollup as rollup

    state = {"reply": CONVERSATIONAL_REPLY, "calls": 0}

    def fake_query(system, user, model, **kw):
        state["calls"] += 1
        return state["reply"]

    monkeypatch.setattr(rollup, "query", fake_query)
    monkeypatch.setattr(rollup, "read_project_context", lambda p: "")
    monkeypatch.setattr(rollup, "git_remote_url", lambda p: None)
    monkeypatch.setattr(rollup, "claude_projects_root", lambda: tmp_path / "no-memory")
    return state


def _seed_project(out_root: Path) -> Path:
    project_md = _project_dir(out_root) / "project.md"
    project_md.parent.mkdir(parents=True, exist_ok=True)
    project_md.write_text(PRIOR_PROJECT_MD)
    return project_md


def test_rollup_refusal_leaves_the_session_unclaimed(
    tmp_path: Path, fake_llm, rollup_reply
):
    src = _write_session(tmp_path / "src")
    out = tmp_path / "out"
    project_md = _seed_project(out)
    ledger = DigestLedger(tmp_path / "state.json")

    outcome = digest_session(src, _config(out, skip_rollup=False), ledger=ledger)

    assert outcome.status == "rollup_refused"
    assert outcome.out_path is not None and outcome.out_path.exists()
    assert project_md.read_text() == PRIOR_PROJECT_MD
    # Nothing was recorded, so the next pass re-claims the session.
    assert ledger.sessions == {}
    assert ledger.projects == {}


def test_rollup_refusal_retries_the_session_on_the_next_pass(
    tmp_path: Path, fake_llm, rollup_reply
):
    """The reviewer's exact scenario: pass two must not skip the session."""
    src = _write_session(tmp_path / "src")
    out = tmp_path / "out"
    project_md = _seed_project(out)
    ledger = DigestLedger(tmp_path / "state.json")
    config = _config(out, skip_rollup=False)

    first = digest_session(src, config, ledger=ledger)
    second = digest_session(src, config, ledger=ledger)

    # The data-loss shape: pass two treating the session as done.
    assert second.status != "skipped"
    assert first.status == "rollup_refused"
    assert second.status == "rollup_refused"
    assert second.reason and "already digested" not in second.reason
    # The roll-up was really attempted twice, not skipped as already done.
    assert rollup_reply["calls"] == 2
    assert len(fake_llm) == 2
    assert project_md.read_text() == PRIOR_PROJECT_MD


def test_the_refused_session_lands_in_project_md_once_the_model_behaves(
    tmp_path: Path, fake_llm, rollup_reply
):
    """The point of retrying: the work is deferred, not lost."""
    src = _write_session(tmp_path / "src")
    out = tmp_path / "out"
    project_md = _seed_project(out)
    ledger = DigestLedger(tmp_path / "state.json")
    config = _config(out, skip_rollup=False)

    assert digest_session(src, config, ledger=ledger).status == "rollup_refused"

    rollup_reply["reply"] = VALID_ROLLUP_OUTPUT
    third = digest_session(src, config, ledger=ledger)

    assert third.status == "digested"
    assert "Azure Container Apps" in project_md.read_text()
    assert ledger.sessions != {}
    assert ledger.projects != {}
    # Now that it landed, the session is done.
    assert digest_session(src, config, ledger=ledger).status == "skipped"


def test_rollup_refusal_keeps_the_session_digest_on_disk(
    tmp_path: Path, fake_llm, rollup_reply
):
    """The digest is paid for and correct; only project.md is behind."""
    src = _write_session(tmp_path / "src")
    out = tmp_path / "out"
    _seed_project(out)

    outcome = digest_session(
        src, _config(out, skip_rollup=False), ledger=DigestLedger(tmp_path / "s.json")
    )

    body = outcome.out_path.read_text()
    assert "## Summary" in body
    assert "A concise summary." in body


def test_rollup_refusal_on_a_cold_project_writes_no_project_md(
    tmp_path: Path, fake_llm, rollup_reply
):
    src = _write_session(tmp_path / "src")
    out = tmp_path / "out"

    outcome = digest_session(src, _config(out, skip_rollup=False))

    assert outcome.status == "rollup_refused"
    assert not (_project_dir(out) / "project.md").exists()


def test_rollup_refusal_is_reported_not_swallowed(
    tmp_path: Path, fake_llm, rollup_reply, capsys
):
    src = _write_session(tmp_path / "src")
    out = tmp_path / "out"
    _seed_project(out)

    digest_session(src, _config(out, skip_rollup=False))

    printed = capsys.readouterr().out
    assert "REFUSED" in printed
    assert "unclaimed" in printed or "retries" in printed


def test_other_rollup_failures_still_commit_the_session_claim(
    tmp_path: Path, fake_llm, monkeypatch
):
    """Only a refusal defers the session; an ordinary failure does not."""

    def boom(*a, **kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr(pipeline, "roll_up_project", boom)
    monkeypatch.setattr(pipeline, "update_index", lambda *_a, **_k: None)

    src = _write_session(tmp_path / "src")
    out = tmp_path / "out"
    ledger = DigestLedger(tmp_path / "state.json")

    first = digest_session(src, _config(out, skip_rollup=False), ledger=ledger)
    second = digest_session(src, _config(out, skip_rollup=False), ledger=ledger)

    assert first.status == "digested"
    assert second.status == "skipped"


def test_rate_limit_in_the_rollup_commits_the_digest_then_raises(
    tmp_path: Path, fake_llm, monkeypatch
):
    """The session digest is already paid for, so it is recorded."""

    def boom(*a, **kw):
        raise SagentRateLimitError("hit it")

    monkeypatch.setattr(pipeline, "roll_up_project", boom)
    monkeypatch.setattr(pipeline, "update_index", lambda *_a, **_k: None)

    src = _write_session(tmp_path / "src")
    out = tmp_path / "out"
    ledger = DigestLedger(tmp_path / "state.json")

    with pytest.raises(SagentRateLimitError):
        digest_session(src, _config(out, skip_rollup=False), ledger=ledger)

    assert ledger.sessions != {}


# ---------------------------------------------------------------------------
# GROUPS.md in the production digest flow (F6)
# ---------------------------------------------------------------------------


def test_a_no_llm_digest_makes_no_llm_call_at_all(
    tmp_path: Path, fake_llm, rollup_reply
):
    """`--no-llm` must stay offline: no digest call and no grouping call."""
    src = _write_session(tmp_path / "src")
    out = tmp_path / "out"
    _seed_project(out)

    outcome = digest_session(src, _config(out, skip_rollup=False, no_llm=True))

    assert outcome.mode == "no_llm"
    assert fake_llm == []
    assert rollup_reply["calls"] == 0
    assert not (out / "GROUPS.md").exists()


def test_the_digest_flow_asks_for_a_groups_refresh(
    tmp_path: Path, fake_llm, monkeypatch
):
    """GROUPS.md is meant to fall out of the normal flow, not out of a flag."""
    calls: list[dict] = []

    monkeypatch.setattr(pipeline, "roll_up_project", lambda *a, **kw: None)
    monkeypatch.setattr(pipeline, "detect_rebrands", lambda *a, **kw: [])
    monkeypatch.setattr(
        pipeline,
        "update_index",
        lambda root, **kw: calls.append(kw) or None,
    )

    src = _write_session(tmp_path / "src")
    out = tmp_path / "out"
    config = _config(out, skip_rollup=False)
    digest_session(src, config)

    assert calls, "the roll-up must refresh the index"
    assert calls[0].get("groups_model") == config.model


def test_rollup_refused_is_a_declared_digest_status():
    from sagent.pipeline import DigestOutcome

    outcome = DigestOutcome(status="rollup_refused", session_path=Path("/x"))
    assert outcome.status == "rollup_refused"


# ---------------------------------------------------------------------------
# Opencode top-level guard (F9) — must fail CLOSED
# ---------------------------------------------------------------------------

PARENT_ID = "ses_parent00000000000000000"
CHILD_ID = "ses_child000000000000000000"


def _rows(*ids: str):
    from sagent import opencode

    return [
        opencode.SessionRow(
            session_id=sid, directory="/x/y", project_id="global", time_updated=1
        )
        for sid in ids
    ]


def test_top_level_refusal_allows_a_listed_session(tmp_path: Path, monkeypatch):
    from sagent import opencode

    monkeypatch.setattr(opencode, "list_sessions", lambda _db: _rows(PARENT_ID))
    assert pipeline._top_level_refusal(tmp_path / "db", PARENT_ID) is None


def test_top_level_refusal_rejects_a_child_session(tmp_path: Path, monkeypatch):
    from sagent import opencode

    monkeypatch.setattr(opencode, "list_sessions", lambda _db: _rows(PARENT_ID))
    reason = pipeline._top_level_refusal(tmp_path / "db", CHILD_ID)
    assert reason is not None
    assert "top-level" in reason


def test_top_level_refusal_fails_closed_when_the_database_is_unreadable(
    tmp_path: Path, monkeypatch
):
    """An unreadable database used to read as 'this is top-level'."""
    from sagent import opencode

    def boom(_db):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(opencode, "list_sessions", boom)
    reason = pipeline._top_level_refusal(tmp_path / "db", CHILD_ID)

    assert reason is not None
    # The reason is honest about why, not the wrong "not top-level" answer.
    assert "unreadable" in reason
    assert "database is locked" in reason


def test_unreadable_database_drops_a_child_instead_of_digesting_it(
    tmp_path: Path, fake_llm, monkeypatch
):
    """F9 end to end: no duplicate document, and nothing is claimed."""
    from sagent import opencode

    def boom(_db):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(opencode, "list_sessions", boom)
    monkeypatch.setattr(
        opencode, "load_session", lambda *_a, **_k: pytest.fail("must not load")
    )

    out = tmp_path / "out"
    ledger = DigestLedger(tmp_path / "state.json")
    outcome = pipeline.digest_opencode_session(
        CHILD_ID,
        _config(out, skip_rollup=False),
        db_path=tmp_path / "opencode.db",
        directory="/x/y",
        size=4096,
        ledger=ledger,
    )

    assert outcome.status == "dropped"
    assert outcome.harness == "opencode"
    assert outcome.reason and "refusing to guess" in outcome.reason
    assert fake_llm == []
    # Nothing was claimed, so one retry is the whole cost.
    assert ledger.sessions == {}
    assert not out.exists()


def test_a_child_session_is_dropped_when_the_database_reads_fine(
    tmp_path: Path, fake_llm, monkeypatch
):
    from sagent import opencode

    monkeypatch.setattr(opencode, "list_sessions", lambda _db: _rows(PARENT_ID))
    monkeypatch.setattr(
        opencode, "load_session", lambda *_a, **_k: pytest.fail("must not load")
    )

    outcome = pipeline.digest_opencode_session(
        CHILD_ID,
        _config(tmp_path / "out"),
        db_path=tmp_path / "opencode.db",
        directory="/x/y",
        size=4096,
    )

    assert outcome.status == "dropped"
    assert outcome.reason == "not a top-level opencode session"
    assert fake_llm == []
