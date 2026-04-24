from __future__ import annotations

import argparse
import os
import socket
import sys
from pathlib import Path

from .digest import write_timeline
from .parser import load_session
from .understand import write_understanding
from .watcher import (
    CLAUDE_PROJECTS,
    latest_session,
    project_dir_for_cwd,
    watch_all,
    watch_project,
)


def default_out_dir() -> Path:
    """Compute the default output root.

    Precedence: $SAGENT_OUT > ~/Obsidian/sagent/<hostname> > ./sagent-out
    """
    env = os.environ.get("SAGENT_OUT")
    if env:
        return Path(env).expanduser()
    obsidian = Path.home() / "Obsidian"
    if obsidian.is_dir():
        return obsidian / "sagent" / socket.gethostname()
    return Path("sagent-out")


def _clean_project_name(dir_name: str) -> str:
    """Strip the '-home-<user>-src-' prefix from project dir names for readability."""
    home = str(Path.home()).replace("/", "-")
    if dir_name.startswith(home + "-"):
        return dir_name[len(home) + 1 :]
    return dir_name.lstrip("-")


def _resolve_input(arg: str | None) -> Path:
    if arg is None:
        latest = latest_session(project_dir_for_cwd(Path.cwd()))
        if latest is None:
            sys.exit("no sessions found for current cwd")
        return latest
    p = Path(arg).expanduser()
    if p.is_file():
        return p
    if p.is_dir():
        latest = latest_session(p)
        if latest is None:
            sys.exit(f"no .jsonl sessions in {p}")
        return latest
    encoded = project_dir_for_cwd(arg)
    if encoded.exists():
        latest = latest_session(encoded)
        if latest is None:
            sys.exit(f"no .jsonl sessions in {encoded}")
        return latest
    sys.exit(f"could not resolve: {arg}")


def _out_dir_for(session_path: Path, base: Path) -> Path:
    return base / _clean_project_name(session_path.parent.name) / session_path.stem


def _digest_one(
    session_path: Path,
    out_root: Path,
    *,
    model: str,
    backend: str,
    no_llm: bool,
    verbose: bool = True,
) -> None:
    session = load_session(session_path)
    out = _out_dir_for(session_path, out_root)
    if verbose:
        print(f"[sagent] {session_path.name} → {out}")
    write_timeline(session, out)
    if not no_llm:
        try:
            write_understanding(session, out, model=model, backend=backend)
        except RuntimeError as exc:
            print(f"[sagent] understanding failed for {session_path.name}: {exc}")


def cmd_digest(args: argparse.Namespace) -> int:
    session_path = _resolve_input(args.target)
    out_root = Path(args.out) if args.out else default_out_dir()
    session = load_session(session_path)
    out = _out_dir_for(session_path, out_root)
    print(f"[sagent] {session_path} → {out}")
    tl = write_timeline(session, out)
    print(f"  ✓ {tl}")
    if not args.no_llm:
        backend = args.backend
        if backend == "auto":
            backend = "sdk" if os.environ.get("ANTHROPIC_API_KEY") else "cli"
        print(f"  … running understanding via {backend}")
        try:
            summary, understanding = write_understanding(
                session, out, model=args.model, backend=args.backend
            )
            print(f"  ✓ {summary}")
            print(f"  ✓ {understanding}")
        except RuntimeError as exc:
            print(f"  ! understanding failed: {exc}")
            return 1
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    out_root = Path(args.out) if args.out else default_out_dir()

    def on_change(path: Path) -> None:
        _digest_one(
            path,
            out_root,
            model=args.model,
            backend=args.backend,
            no_llm=args.no_llm,
        )
        print(f"[sagent] digested → {_out_dir_for(path, out_root)}")

    if args.target:
        p = Path(args.target).expanduser()
        if p.is_file():
            from .watcher import watch as watch_file

            watch_file(p, on_change)
            return 0
        project_dir = p if p.is_dir() else project_dir_for_cwd(args.target)
    else:
        project_dir = project_dir_for_cwd(Path.cwd())

    watch_project(project_dir, on_change)
    return 0


def cmd_watch_all(args: argparse.Namespace) -> int:
    out_root = Path(args.out) if args.out else default_out_dir()
    print(f"[sagent] output root: {out_root}")

    def on_change(path: Path) -> None:
        _digest_one(
            path,
            out_root,
            model=args.model,
            backend=args.backend,
            no_llm=args.no_llm,
        )

    watch_all(on_change, min_bytes=args.min_bytes)
    return 0


def cmd_digest_all(args: argparse.Namespace) -> int:
    out_root = Path(args.out) if args.out else default_out_dir()
    print(f"[sagent] output root: {out_root}")
    min_bytes = args.min_bytes
    count = 0
    for proj in sorted(CLAUDE_PROJECTS.iterdir()):
        if not proj.is_dir():
            continue
        for sess in sorted(proj.glob("*.jsonl")):
            if sess.stat().st_size < min_bytes:
                continue
            _digest_one(
                sess,
                out_root,
                model=args.model,
                backend=args.backend,
                no_llm=args.no_llm,
            )
            count += 1
    print(f"[sagent] digested {count} sessions")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    root = CLAUDE_PROJECTS
    if not root.exists():
        sys.exit(f"no claude projects dir at {root}")
    for proj in sorted(root.iterdir()):
        if not proj.is_dir():
            continue
        sessions = sorted(proj.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
        if not sessions:
            continue
        print(f"{proj.name}  ({len(sessions)} sessions)")
        if args.verbose:
            for s in sessions[-3:]:
                print(f"  {s.name}  {s.stat().st_size:>10} bytes")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="sagent", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    common_model = dict(default="claude-sonnet-4-6")
    common_backend = dict(
        default="auto",
        choices=["auto", "sdk", "cli"],
        help="LLM backend: auto=SDK if ANTHROPIC_API_KEY set else CLI subscription",
    )
    out_help = (
        "output root (default: $SAGENT_OUT or ~/Obsidian/sagent/<hostname>/ "
        "or ./sagent-out)"
    )

    pd = sub.add_parser("digest", help="digest a single session JSONL")
    pd.add_argument(
        "target",
        nargs="?",
        help="path to .jsonl, project dir, or cwd; default = current cwd's latest",
    )
    pd.add_argument("--out", default=None, help=out_help)
    pd.add_argument("--model", **common_model)
    pd.add_argument("--backend", **common_backend)
    pd.add_argument("--no-llm", action="store_true", help="skip LLM understanding")
    pd.set_defaults(func=cmd_digest)

    pda = sub.add_parser("digest-all", help="digest every session across all projects")
    pda.add_argument("--out", default=None, help=out_help)
    pda.add_argument("--model", **common_model)
    pda.add_argument("--backend", **common_backend)
    pda.add_argument("--no-llm", action="store_true")
    pda.add_argument(
        "--min-bytes",
        type=int,
        default=5000,
        help="skip sessions smaller than this many bytes (default: 5000)",
    )
    pda.set_defaults(func=cmd_digest_all)

    pw = sub.add_parser("watch", help="watch a project or file and digest on change")
    pw.add_argument("target", nargs="?")
    pw.add_argument("--out", default=None, help=out_help)
    pw.add_argument("--model", **common_model)
    pw.add_argument("--backend", **common_backend)
    pw.add_argument("--no-llm", action="store_true")
    pw.set_defaults(func=cmd_watch)

    pwa = sub.add_parser(
        "watch-all", help="watch every project in ~/.claude/projects/"
    )
    pwa.add_argument("--out", default=None, help=out_help)
    pwa.add_argument("--model", **common_model)
    pwa.add_argument("--backend", **common_backend)
    pwa.add_argument("--no-llm", action="store_true")
    pwa.add_argument(
        "--min-bytes",
        type=int,
        default=5000,
        help="skip sessions smaller than this many bytes (default: 5000)",
    )
    pwa.set_defaults(func=cmd_watch_all)

    pl = sub.add_parser("list", help="list Claude Code projects with sessions")
    pl.add_argument("-v", "--verbose", action="store_true")
    pl.set_defaults(func=cmd_list)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
