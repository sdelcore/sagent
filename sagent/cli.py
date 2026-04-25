from __future__ import annotations

import argparse
import os
import socket
import sys
from pathlib import Path

from .digest import write_timeline
from .parser import load_session
from .state import StateStore, default_state_path
from .understand import write_understanding
from .watcher import (
    CLAUDE_PROJECTS,
    DEFAULT_QUIET_SECONDS,
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


def _read_prior(out_dir: Path) -> tuple[str, str]:
    s = out_dir / "summary.md"
    u = out_dir / "understanding.md"
    return (
        s.read_text() if s.exists() else "",
        u.read_text() if u.exists() else "",
    )


def _digest_one(
    session_path: Path,
    out_root: Path,
    *,
    model: str,
    no_llm: bool,
    state: StateStore | None,
    force_full: bool = False,
    full_rebuild_every: int = 10,
    min_delta: int = 0,
    verbose: bool = True,
) -> None:
    try:
        current_size = session_path.stat().st_size
    except FileNotFoundError:
        return

    if (
        state is not None
        and not force_full
        and state.should_skip(session_path, size=current_size, min_delta=min_delta)
    ):
        if verbose:
            print(f"[sagent] {session_path.name} already digested, skipping")
        return

    session = load_session(session_path)
    out = _out_dir_for(session_path, out_root)
    if verbose:
        print(f"[sagent] {session_path.name} → {out}")
    write_timeline(session, out)

    if no_llm:
        if state is not None:
            state.mark_digested(
                session_path,
                size=current_size,
                event_index=len(session.events),
            )
            state.save()
        return

    rec = state.get(session_path) if state is not None else None
    digest_count = rec.digest_count if rec else 0

    do_incremental = (
        rec is not None
        and rec.last_event_index > 0
        and rec.last_event_index < len(session.events)
        and not force_full
        and (full_rebuild_every <= 0 or (digest_count + 1) % full_rebuild_every != 0)
    )

    if do_incremental:
        prior_summary, prior_understanding = _read_prior(out)
        if not prior_summary.strip():
            do_incremental = False  # missing prior on disk; fall back to cold

    try:
        if do_incremental:
            assert rec is not None
            new_count = len(session.events) - rec.last_event_index
            if verbose:
                print(
                    f"  … incremental update ({new_count} new events, "
                    f"prior at index {rec.last_event_index})"
                )
            write_understanding(
                session,
                out,
                model=model,
                prior_summary=prior_summary,
                prior_understanding=prior_understanding,
                since_event_index=rec.last_event_index,
            )
        else:
            if verbose:
                reason = (
                    "force-full" if force_full
                    else "rebuild cycle" if rec and (digest_count + 1) % full_rebuild_every == 0
                    else "cold start"
                )
                print(f"  … full digest ({reason})")
            write_understanding(session, out, model=model)
    except Exception as exc:
        print(f"[sagent] understanding failed for {session_path.name}: {exc}")
        return

    if state is not None:
        state.mark_digested(
            session_path,
            size=session_path.stat().st_size,
            event_index=len(session.events),
        )
        state.save()


def _make_state(args: argparse.Namespace) -> StateStore | None:
    if getattr(args, "no_state", False):
        return None
    return StateStore(Path(args.state) if args.state else None)


def cmd_digest(args: argparse.Namespace) -> int:
    session_path = _resolve_input(args.target)
    out_root = Path(args.out) if args.out else default_out_dir()
    state = _make_state(args)
    _digest_one(
        session_path,
        out_root,
        model=args.model,
        no_llm=args.no_llm,
        state=state,
        force_full=args.force_full,
        full_rebuild_every=args.full_rebuild_every,
    )
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    out_root = Path(args.out) if args.out else default_out_dir()
    state = _make_state(args)

    def on_change(path: Path) -> None:
        _digest_one(
            path,
            out_root,
            model=args.model,
            no_llm=args.no_llm,
            state=state,
            force_full=args.force_full,
            full_rebuild_every=args.full_rebuild_every,
        )
        print(f"[sagent] digested → {_out_dir_for(path, out_root)}")

    if args.target:
        p = Path(args.target).expanduser()
        if p.is_file():
            from .watcher import watch as watch_file

            watch_file(p, on_change, quiet_seconds=args.idle_seconds)
            return 0
        project_dir = p if p.is_dir() else project_dir_for_cwd(args.target)
    else:
        project_dir = project_dir_for_cwd(Path.cwd())

    watch_project(project_dir, on_change, quiet_seconds=args.idle_seconds)
    return 0


def cmd_watch_all(args: argparse.Namespace) -> int:
    out_root = Path(args.out) if args.out else default_out_dir()
    state = _make_state(args)
    print(f"[sagent] output root: {out_root}")
    if state is not None:
        print(f"[sagent] state: {state.path}")

    def on_change(path: Path) -> None:
        _digest_one(
            path,
            out_root,
            model=args.model,
            no_llm=args.no_llm,
            state=state,
            force_full=args.force_full,
            full_rebuild_every=args.full_rebuild_every,
        )

    watch_all(
        on_change,
        min_bytes=args.min_bytes,
        min_delta=args.min_delta,
        quiet_seconds=args.idle_seconds,
        state=state,
    )
    return 0


def cmd_digest_all(args: argparse.Namespace) -> int:
    out_root = Path(args.out) if args.out else default_out_dir()
    state = _make_state(args)
    print(f"[sagent] output root: {out_root}")
    if state is not None:
        print(f"[sagent] state: {state.path}")
    count = 0
    skipped = 0
    for proj in sorted(CLAUDE_PROJECTS.iterdir()):
        if not proj.is_dir():
            continue
        for sess in sorted(proj.glob("*.jsonl")):
            try:
                size = sess.stat().st_size
            except FileNotFoundError:
                continue
            if size < args.min_bytes:
                continue
            if state is not None and state.should_skip(
                sess, size=size, min_delta=args.min_delta
            ):
                skipped += 1
                continue
            _digest_one(
                sess,
                out_root,
                model=args.model,
                no_llm=args.no_llm,
                state=state,
                force_full=args.force_full,
                full_rebuild_every=args.full_rebuild_every,
            )
            count += 1
    print(f"[sagent] digested {count}; skipped {skipped} already-digested")
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


def _add_state_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--state",
        default=None,
        help=f"state file path (default: $SAGENT_STATE or {default_state_path()})",
    )
    p.add_argument(
        "--no-state",
        action="store_true",
        help="don't read or write state — every run is cold",
    )
    p.add_argument(
        "--force-full",
        action="store_true",
        help="rebuild summary from full transcript, ignore prior",
    )
    p.add_argument(
        "--full-rebuild-every",
        type=int,
        default=10,
        help=(
            "force a full rebuild every N digests of a session "
            "(default: 10; 0 disables)"
        ),
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="sagent", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    common_model = dict(default="claude-haiku-4-5")
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
    pd.add_argument("--no-llm", action="store_true", help="skip LLM understanding")
    _add_state_args(pd)
    pd.set_defaults(func=cmd_digest)

    pda = sub.add_parser("digest-all", help="digest every session across all projects")
    pda.add_argument("--out", default=None, help=out_help)
    pda.add_argument("--model", **common_model)
    pda.add_argument("--no-llm", action="store_true")
    pda.add_argument(
        "--min-bytes",
        type=int,
        default=5000,
        help="skip sessions smaller than this many bytes (default: 5000)",
    )
    pda.add_argument(
        "--min-delta",
        type=int,
        default=0,
        help="skip if file grew less than this many bytes since last digest",
    )
    _add_state_args(pda)
    pda.set_defaults(func=cmd_digest_all)

    pw = sub.add_parser("watch", help="watch a project or file and digest on change")
    pw.add_argument("target", nargs="?")
    pw.add_argument("--out", default=None, help=out_help)
    pw.add_argument("--model", **common_model)
    pw.add_argument("--no-llm", action="store_true")
    pw.add_argument(
        "--idle-seconds",
        type=float,
        default=DEFAULT_QUIET_SECONDS,
        help=f"idle threshold before digesting (default: {DEFAULT_QUIET_SECONDS:.0f}s)",
    )
    _add_state_args(pw)
    pw.set_defaults(func=cmd_watch)

    pwa = sub.add_parser(
        "watch-all", help="watch every project in ~/.claude/projects/"
    )
    pwa.add_argument("--out", default=None, help=out_help)
    pwa.add_argument("--model", **common_model)
    pwa.add_argument("--no-llm", action="store_true")
    pwa.add_argument(
        "--min-bytes",
        type=int,
        default=5000,
        help="skip sessions smaller than this many bytes (default: 5000)",
    )
    pwa.add_argument(
        "--min-delta",
        type=int,
        default=0,
        help="skip if file grew less than this many bytes since last digest",
    )
    pwa.add_argument(
        "--idle-seconds",
        type=float,
        default=DEFAULT_QUIET_SECONDS,
        help=f"idle threshold before digesting (default: {DEFAULT_QUIET_SECONDS:.0f}s)",
    )
    _add_state_args(pwa)
    pwa.set_defaults(func=cmd_watch_all)

    pl = sub.add_parser("list", help="list Claude Code projects with sessions")
    pl.add_argument("-v", "--verbose", action="store_true")
    pl.set_defaults(func=cmd_list)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
