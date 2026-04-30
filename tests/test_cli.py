from __future__ import annotations

from pathlib import Path

from sagent.cli import default_out_dir
from sagent.pipeline import clean_project_name


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


def test_default_out_dir_obsidian(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("SAGENT_OUT", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / "Obsidian").mkdir()
    out = default_out_dir()
    assert out == tmp_path / "Obsidian" / "sagent" / __import__("socket").gethostname()


def test_default_out_dir_fallback(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("SAGENT_OUT", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    # no Obsidian dir
    assert default_out_dir() == Path("sagent-out")
