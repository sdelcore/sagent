"""Detect project rebrands by matching git remote URLs across digests.

sagent keys projects by filesystem path. When a repo is renamed or
transferred (`durandal-systems/core` → `tensile-systems`), the old path's
digest goes stale while the new path's digest starts from scratch — and the
two are silently unrelated, so the cross-walk to the owner's vault breaks.

We capture each project's git remote URL into its front matter at roll-up
time, then flag any two project keys that share a remote. This is
flag-only: digests are never merged. The relationship is surfaced
symmetrically — every project sharing a remote carries the same
`rebrand_detected: <old-key(s)> → <new-key>` line, where the newest by
`last_updated` is treated as the canonical key. The rule is uniform: the
remote URL is the identity, so same-org renames and cross-org transfers are
treated identically.
"""

from __future__ import annotations

import re
import subprocess
from collections import defaultdict
from pathlib import Path

from .frontmatter import split_front_matter, to_front_matter


def git_remote_url(path: Path | str | None, *, timeout: float = 5.0) -> str | None:
    """Return `git remote get-url origin` for `path`, or None.

    None when the path is missing, isn't a git work tree, has no `origin`
    remote, or git isn't available. Never raises.
    """
    if path is None:
        return None
    p = Path(path)
    if not p.is_dir():
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(p), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    url = result.stdout.strip()
    return url or None


def normalize_remote_url(url: str | None) -> str:
    """Canonicalize a remote URL to `host/owner/repo` for identity matching.

    Collapses the forms git uses for the same repo — `git@host:owner/repo.git`,
    `https://host/owner/repo.git`, `ssh://git@host:22/owner/repo` — to a single
    lowercased key. Returns "" for empty/unparseable input.
    """
    u = (url or "").strip()
    if not u:
        return ""
    u = re.sub(r"^[A-Za-z][A-Za-z0-9+.\-]*://", "", u)  # scheme
    u = re.sub(r"^[^@/]+@", "", u)  # user@
    u = re.sub(r":(\d+)(?=/)", "", u)  # :port before a path
    u = u.replace(":", "/")  # scp-form host:path → host/path
    u = re.sub(r"/+", "/", u).rstrip("/")
    if u.endswith(".git"):
        u = u[:-4]
    return u.lower()


def _rebrand_line(keys: list[str], last_updated_of: dict[str, str]) -> str:
    """Build the `<old(s)> → <new>` line shared by every member of a group.

    The newest key by `last_updated` (lexical on the ISO timestamp, project
    name as tiebreak) is the canonical "new" key; the rest are listed as old.
    """
    uniq = sorted(set(keys))
    new = max(uniq, key=lambda k: (last_updated_of.get(k, ""), k))
    old = [k for k in uniq if k != new]
    return f"{', '.join(old)} → {new}"


def detect_rebrands(out_root: Path) -> list[tuple[str, str | None]]:
    """Flag project digests that share a git remote with another project.

    Scans every `<project>/project.md` under `out_root`, groups them by
    normalized remote URL, and writes a shared `rebrand_detected` line into
    the front matter of each project in a collision group. Stale flags (a
    project that no longer collides) are removed. Only files whose flag
    actually changes are rewritten; the body is preserved untouched.

    Returns the list of `(project_key, new_flag_value)` that were rewritten,
    where a value of None means the flag was cleared — useful for logging.
    """
    if not out_root.is_dir():
        return []

    project_files: dict[str, tuple[Path, dict, str]] = {}
    by_remote: dict[str, list[str]] = defaultdict(list)
    last_updated_of: dict[str, str] = {}

    for proj_dir in sorted(out_root.iterdir()):
        if not proj_dir.is_dir():
            continue
        pf = proj_dir / "project.md"
        if not pf.exists():
            continue
        fm, body = split_front_matter(pf.read_text(errors="ignore"))
        if not fm or fm.get("type") != "project":
            continue
        key = proj_dir.name
        project_files[key] = (pf, fm, body)
        last_updated_of[key] = str(fm.get("last_updated") or "")
        norm = normalize_remote_url(fm.get("remote_url"))
        if norm:
            by_remote[norm].append(key)

    desired: dict[str, str | None] = {key: None for key in project_files}
    for keys in by_remote.values():
        if len(set(keys)) < 2:
            continue
        line = _rebrand_line(keys, last_updated_of)
        for k in keys:
            desired[k] = line

    changed: list[tuple[str, str | None]] = []
    for key, (pf, fm, body) in project_files.items():
        want = desired[key]
        have = fm.get("rebrand_detected")
        if want == have:
            continue
        if want is None:
            fm.pop("rebrand_detected", None)
        else:
            fm["rebrand_detected"] = want
        pf.write_text(to_front_matter(fm) + "\n" + body)
        changed.append((key, want))

    return changed
