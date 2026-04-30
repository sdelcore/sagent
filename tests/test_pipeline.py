from __future__ import annotations

import json
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
