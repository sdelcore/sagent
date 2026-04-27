"""Read anchor files from a project root to give the digest LLM grounding.

Used by the project rollup so the LLM can describe what a project IS
(not just what was discussed in sessions). Top-level anchors only — no
recursion, no source dumps, no source files.
"""

from __future__ import annotations

from pathlib import Path

# Files we look for in the project root, in priority order.
# README first because it's the highest-signal grounding for a description.
_ANCHOR_FILES = [
    "README.md",
    "README.rst",
    "README.txt",
    "README",
    "CLAUDE.md",
    "AGENTS.md",
    "package.json",
    "pyproject.toml",
    "Cargo.toml",
    "flake.nix",
    "go.mod",
    "Justfile",
    "justfile",
    "Makefile",
    "docker-compose.yml",
    "docker-compose.yaml",
    "Dockerfile",
]

# Hidden entries we still surface in the listing because they're
# load-bearing on this user's machines.
_KEEP_HIDDEN = {".envrc", ".gitignore", ".dockerignore", ".tool-versions"}

# Directory entries we never list (noise).
_HIDE_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    "target",
    "dist",
    "build",
    "result",
    ".direnv",
    ".cache",
    ".next",
    ".turbo",
    ".pytest_cache",
}


def read_project_context(
    path: Path | str | None,
    *,
    max_total_chars: int = 20_000,
    per_file_chars: int = 4_000,
    max_listing_entries: int = 80,
) -> str:
    """Return a markdown context blob for the LLM, or "" if path is missing.

    Includes a top-level entry listing and the contents of the first few
    anchor files that exist. Capped per-file and overall.
    """
    if path is None:
        return ""
    p = Path(path)
    if not p.exists() or not p.is_dir():
        return ""

    parts: list[str] = []
    total = 0

    listing_block = _build_listing(p, max_listing_entries)
    if listing_block:
        parts.append(listing_block)
        total += len(listing_block)

    for fname in _ANCHOR_FILES:
        if total >= max_total_chars:
            break
        fp = p / fname
        if not fp.is_file():
            continue
        try:
            content = fp.read_text(errors="ignore")
        except OSError:
            continue
        if len(content) > per_file_chars:
            content = content[:per_file_chars].rstrip() + "\n\n... [truncated]"
        block = f"## {fname}\n\n```\n{content}\n```"
        remaining = max_total_chars - total
        if len(block) > remaining:
            block = block[: remaining - 20].rstrip() + "\n... [truncated]\n```"
        parts.append(block)
        total += len(block)

    return ("\n\n".join(parts)).strip()


def _build_listing(path: Path, max_entries: int) -> str:
    try:
        entries = list(path.iterdir())
    except OSError:
        return ""
    visible: list[str] = []
    for f in sorted(entries, key=lambda x: x.name.lower()):
        name = f.name
        if f.is_dir():
            if name in _HIDE_DIRS:
                continue
            if name.startswith("."):
                continue
            visible.append(name + "/")
        else:
            if name.startswith(".") and name not in _KEEP_HIDDEN:
                continue
            visible.append(name)
    if not visible:
        return ""
    truncated = visible[:max_entries]
    suffix = (
        f"\n- ... ({len(visible) - max_entries} more)"
        if len(visible) > max_entries
        else ""
    )
    body = "\n".join(f"- `{n}`" for n in truncated) + suffix
    return f"## Top-level entries\n\n{body}"
