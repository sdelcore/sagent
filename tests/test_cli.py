from __future__ import annotations

from pathlib import Path

import pytest

from sagent import cli
from sagent.cli import _digest_exit_code, default_out_dir
from sagent.pipeline import DigestOutcome, clean_project_name


def test_clean_project_name_strips_home():
    home = str(Path.home()).replace("/", "-")
    name = f"{home}-src-proj"
    assert clean_project_name(name) == "src-proj"


def test_clean_project_name_keeps_odd_names():
    assert clean_project_name("-tmp") == "tmp"
    assert clean_project_name("other") == "other"


def test_default_out_dir_env(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("SAGENT_OUT", str(tmp_path / "o"))
    assert default_out_dir() == tmp_path / "o"


def test_default_out_dir_is_a_dot_dir_under_home(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("SAGENT_OUT", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    out = default_out_dir()
    assert out == tmp_path / ".sagent" / __import__("socket").gethostname()


def test_default_out_dir_ignores_an_obsidian_vault(monkeypatch, tmp_path: Path):
    """The default must not depend on what else is installed.

    sagent used to write into ~/Obsidian when that directory happened to
    exist, so the same command landed in different places on different
    machines. A vault is now opt-in, never sniffed.
    """
    monkeypatch.delenv("SAGENT_OUT", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / "Obsidian").mkdir()
    assert default_out_dir() == tmp_path / ".sagent" / __import__("socket").gethostname()


# ---------------------------------------------------------------------------
# Exit codes for a refused roll-up (D13, F8)
# ---------------------------------------------------------------------------


def test_digest_exit_code_flags_a_refused_roll_up():
    outcome = DigestOutcome(status="rollup_refused", session_path=Path("/x"))
    assert _digest_exit_code(outcome) == 1


@pytest.mark.parametrize("status", ["digested", "skipped", "dropped"])
def test_digest_exit_code_is_zero_otherwise(status: str):
    outcome = DigestOutcome(status=status, session_path=Path("/x"))
    assert _digest_exit_code(outcome) == 0


# ---------------------------------------------------------------------------
# GROUPS.md from the CLI (F6)
# ---------------------------------------------------------------------------

GROUPS_REPLY = (
    "## hms\n"
    "_one deployed system split across two directories_\n"
    "- `hms-atlas` — the API\n"
    "- `hms-rag` — retrieval\n"
)


@pytest.fixture
def groups_llm(monkeypatch):
    """Count grouping calls without reaching the API."""
    import sagent.rollup as rollup

    calls: list[str] = []
    monkeypatch.setattr(
        rollup, "query", lambda *a, **kw: calls.append("called") or GROUPS_REPLY
    )
    return calls


def _seed_two_projects(root: Path) -> None:
    for key in ("hms-atlas", "hms-rag"):
        d = root / key
        d.mkdir(parents=True, exist_ok=True)
        (d / "project.md").write_text(
            "---\n"
            'type: "project"\n'
            f'project: "{key}"\n'
            f'description: "The {key} service."\n'
            'last_updated: "2026-07-28T10:00:00Z"\n'
            "---\n"
            f"# {key}\n"
        )


def test_index_with_groups_writes_groups_md(tmp_path: Path, groups_llm):
    _seed_two_projects(tmp_path)

    assert cli.main(["index", "--out", str(tmp_path), "--groups"]) == 0

    assert len(groups_llm) == 1
    assert (tmp_path / "GROUPS.md").exists()
    assert "## hms" in (tmp_path / "GROUPS.md").read_text()


def test_index_without_groups_makes_no_llm_call(tmp_path: Path, groups_llm):
    _seed_two_projects(tmp_path)

    assert cli.main(["index", "--out", str(tmp_path)]) == 0

    assert groups_llm == []
    assert not (tmp_path / "GROUPS.md").exists()
    assert (tmp_path / "INDEX.md").exists()


def test_index_groups_flag_forces_a_refresh_even_when_fresh(
    tmp_path: Path, groups_llm
):
    """An explicit flag is a request, not a hint. It must not be rate-gated."""
    _seed_two_projects(tmp_path)

    cli.main(["index", "--out", str(tmp_path), "--groups"])
    cli.main(["index", "--out", str(tmp_path), "--groups"])

    assert len(groups_llm) == 2
