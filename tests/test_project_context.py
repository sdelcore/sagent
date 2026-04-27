from __future__ import annotations

from pathlib import Path

from sagent.project_context import read_project_context


def test_returns_empty_for_missing_path(tmp_path: Path):
    assert read_project_context(tmp_path / "nope") == ""


def test_returns_empty_for_none():
    assert read_project_context(None) == ""


def test_returns_empty_for_file_not_dir(tmp_path: Path):
    f = tmp_path / "a.txt"
    f.write_text("hi")
    assert read_project_context(f) == ""


def test_includes_top_level_listing(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "README.md").write_text("# hi\n")
    out = read_project_context(tmp_path)
    assert "## Top-level entries" in out
    assert "src/" in out
    assert "docs/" in out
    assert "README.md" in out


def test_hides_noise_dirs(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "result").mkdir()
    (tmp_path / "src").mkdir()
    out = read_project_context(tmp_path)
    assert "src/" in out
    assert ".git" not in out
    assert "node_modules" not in out
    assert "result/" not in out


def test_keeps_envrc_and_gitignore(tmp_path: Path):
    (tmp_path / ".envrc").write_text("use flake")
    (tmp_path / ".gitignore").write_text("result\n")
    (tmp_path / ".secret").write_text("nope")
    out = read_project_context(tmp_path)
    assert ".envrc" in out
    assert ".gitignore" in out
    assert ".secret" not in out


def test_includes_readme_content(tmp_path: Path):
    (tmp_path / "README.md").write_text("# my project\n\nDoes a thing.")
    out = read_project_context(tmp_path)
    assert "## README.md" in out
    assert "my project" in out
    assert "Does a thing." in out


def test_includes_manifest_files(tmp_path: Path):
    (tmp_path / "package.json").write_text('{"name": "foo"}')
    (tmp_path / "flake.nix").write_text("{ description = \"x\"; }")
    out = read_project_context(tmp_path)
    assert "package.json" in out
    assert "flake.nix" in out
    assert '"name": "foo"' in out


def test_truncates_per_file(tmp_path: Path):
    big = "x" * 100_000
    (tmp_path / "README.md").write_text(big)
    out = read_project_context(tmp_path, per_file_chars=200)
    assert "[truncated]" in out
    # full content should not be present
    assert "x" * 99_000 not in out


def test_respects_total_cap(tmp_path: Path):
    # Multiple large files; total should be capped
    for fn in ("README.md", "package.json", "flake.nix", "pyproject.toml"):
        (tmp_path / fn).write_text("y" * 50_000)
    out = read_project_context(tmp_path, max_total_chars=5_000)
    assert len(out) <= 6_000  # some slack for headers


def test_anchor_priority_order(tmp_path: Path):
    """README section heading should appear before manifest section heading."""
    (tmp_path / "README.md").write_text("readme content")
    (tmp_path / "package.json").write_text('{"k": "v"}')
    out = read_project_context(tmp_path)
    assert out.index("## README.md") < out.index("## package.json")


def test_skips_missing_anchors(tmp_path: Path):
    (tmp_path / "README.md").write_text("ok")
    out = read_project_context(tmp_path)
    # not present, shouldn't error or include
    assert "Cargo.toml" not in out
    assert "go.mod" not in out
