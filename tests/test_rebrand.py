from __future__ import annotations

import subprocess
from pathlib import Path

from sagent.frontmatter import split_front_matter, to_front_matter
from sagent.rebrand import (
    detect_rebrands,
    git_remote_url,
    normalize_remote_url,
)


# ---------------------------------------------------------------------------
# normalize_remote_url
# ---------------------------------------------------------------------------


def test_normalize_collapses_scp_and_https_to_same_key():
    scp = normalize_remote_url("git@github.com:durandal/core.git")
    https = normalize_remote_url("https://github.com/durandal/core.git")
    assert scp == https == "github.com/durandal/core"


def test_normalize_strips_scheme_user_port_and_dotgit():
    assert (
        normalize_remote_url("ssh://git@github.com:22/durandal/core.git")
        == "github.com/durandal/core"
    )


def test_normalize_lowercases_and_trims():
    assert (
        normalize_remote_url("  https://GitHub.com/Durandal/Core/  ")
        == "github.com/durandal/core"
    )


def test_normalize_empty_is_blank():
    assert normalize_remote_url("") == ""
    assert normalize_remote_url(None) == ""


# ---------------------------------------------------------------------------
# git_remote_url
# ---------------------------------------------------------------------------


def test_git_remote_url_none_for_missing_or_nonrepo(tmp_path: Path):
    assert git_remote_url(None) is None
    assert git_remote_url(tmp_path / "does-not-exist") is None
    assert git_remote_url(tmp_path) is None  # dir exists but no git


def test_git_remote_url_reads_origin(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin",
         "git@github.com:durandal/core.git"],
        check=True,
    )
    assert git_remote_url(repo) == "git@github.com:durandal/core.git"


# ---------------------------------------------------------------------------
# detect_rebrands
# ---------------------------------------------------------------------------


def _write_project(
    out_root: Path,
    key: str,
    *,
    remote_url: str | None,
    last_updated: str,
    rebrand_detected: str | None = None,
) -> Path:
    proj = out_root / key
    proj.mkdir(parents=True)
    fm: dict = {
        "type": "project",
        "project": key,
        "remote_url": remote_url,
        "last_updated": last_updated,
    }
    if rebrand_detected is not None:
        fm["rebrand_detected"] = rebrand_detected
    body = f"# {key}\n\nbody text\n"
    (proj / "project.md").write_text(to_front_matter(fm) + "\n" + body)
    return proj / "project.md"


def _flag(path: Path) -> str | None:
    fm, _ = split_front_matter(path.read_text())
    return fm.get("rebrand_detected")


def test_detect_flags_shared_remote_symmetrically(tmp_path: Path):
    old = _write_project(
        tmp_path, "src-durandal-systems-core",
        remote_url="git@github.com:acme/core.git",
        last_updated="2026-04-01T00:00:00Z",
    )
    new = _write_project(
        tmp_path, "src-tensile-systems",
        remote_url="https://github.com/acme/core.git",
        last_updated="2026-06-01T00:00:00Z",
    )
    changed = detect_rebrands(tmp_path)
    assert len(changed) == 2
    line = "src-durandal-systems-core → src-tensile-systems"
    assert _flag(old) == line
    assert _flag(new) == line


def test_detect_picks_newest_as_canonical(tmp_path: Path):
    a = _write_project(
        tmp_path, "src-a", remote_url="git@github.com:acme/x.git",
        last_updated="2026-06-09T00:00:00Z",
    )
    b = _write_project(
        tmp_path, "src-b", remote_url="git@github.com:acme/x.git",
        last_updated="2026-01-01T00:00:00Z",
    )
    detect_rebrands(tmp_path)
    assert _flag(a) == "src-b → src-a"
    assert _flag(b) == "src-b → src-a"


def test_detect_no_flag_for_unique_remotes(tmp_path: Path):
    p = _write_project(
        tmp_path, "src-solo", remote_url="git@github.com:acme/solo.git",
        last_updated="2026-04-01T00:00:00Z",
    )
    _write_project(
        tmp_path, "src-other", remote_url="git@github.com:acme/other.git",
        last_updated="2026-04-01T00:00:00Z",
    )
    assert detect_rebrands(tmp_path) == []
    assert _flag(p) is None


def test_detect_ignores_projects_without_remote(tmp_path: Path):
    _write_project(
        tmp_path, "src-a", remote_url=None,
        last_updated="2026-04-01T00:00:00Z",
    )
    _write_project(
        tmp_path, "src-b", remote_url=None,
        last_updated="2026-04-01T00:00:00Z",
    )
    assert detect_rebrands(tmp_path) == []


def test_detect_clears_stale_flag(tmp_path: Path):
    # Previously flagged, but now the remotes differ — flag must be removed.
    p = _write_project(
        tmp_path, "src-a", remote_url="git@github.com:acme/a.git",
        last_updated="2026-04-01T00:00:00Z",
        rebrand_detected="src-a → src-b",
    )
    changed = detect_rebrands(tmp_path)
    assert changed == [("src-a", None)]
    assert _flag(p) is None


def test_detect_preserves_body(tmp_path: Path):
    old = _write_project(
        tmp_path, "src-a", remote_url="git@github.com:acme/x.git",
        last_updated="2026-04-01T00:00:00Z",
    )
    _write_project(
        tmp_path, "src-b", remote_url="git@github.com:acme/x.git",
        last_updated="2026-06-01T00:00:00Z",
    )
    detect_rebrands(tmp_path)
    _, body = split_front_matter(old.read_text())
    assert body == "# src-a\n\nbody text\n"


def test_detect_is_idempotent(tmp_path: Path):
    _write_project(
        tmp_path, "src-a", remote_url="git@github.com:acme/x.git",
        last_updated="2026-04-01T00:00:00Z",
    )
    _write_project(
        tmp_path, "src-b", remote_url="https://github.com/acme/x.git",
        last_updated="2026-06-01T00:00:00Z",
    )
    assert len(detect_rebrands(tmp_path)) == 2
    assert detect_rebrands(tmp_path) == []  # second pass: nothing to change
