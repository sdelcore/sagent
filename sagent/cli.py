from __future__ import annotations

import argparse
import os
import shutil
import socket
import sys
from pathlib import Path

from .digest import build_timeline, write_session_md
from .parser import load_session
from .rate import RateLimiter, SagentRateLimitError
from .rollup import is_scratchpad, roll_up_project, update_recent
from .state import StateStore, default_state_path
from .understand import run_understanding
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


def _project_dir_for(session_path: Path, base: Path) -> Path:
    """Output dir for the project this session belongs to."""
    return base / _clean_project_name(session_path.parent.name)


def _existing_session_md(project_dir: Path, session_id: str) -> Path | None:
    """Find an existing per-session digest matching this UUID's short id8."""
    sessions_dir = project_dir / "sessions"
    if not sessions_dir.exists():
        return None
    short = session_id.split("-")[0][:8]
    matches = list(sessions_dir.glob(f"*-{short}.md"))
    return matches[0] if matches else None


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
    min_prompts: int = 1,
    skip_rollup: bool = False,
    rate_limiter: RateLimiter | None = None,
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
    project_dir = _project_dir_for(session_path, out_root)

    # Drop sagent's own LLM-call sessions (created when the Agent SDK
    # persisted them to ~/.claude/projects). Without this, the watcher
    # finds them and digests its own prompts, recursing.
    if session.is_sagent_self_generated:
        if verbose:
            print(
                f"[sagent] {session_path.name} is sagent-self-generated, skipping"
            )
        existing = _existing_session_md(project_dir, session.session_id)
        if existing and existing.exists():
            existing.unlink()
        if state is not None:
            state.mark_digested(
                session_path, size=current_size, event_index=len(session.events)
            )
            state.save()
        return

    # Drop empty/trivial sessions
    if len(session.user_prompts) < min_prompts:
        if verbose:
            print(
                f"[sagent] {session_path.name} has {len(session.user_prompts)} "
                f"user prompts (< {min_prompts}), dropping"
            )
        existing = _existing_session_md(project_dir, session.session_id)
        if existing and existing.exists():
            existing.unlink()
        if state is not None:
            state.mark_digested(
                session_path, size=current_size, event_index=len(session.events)
            )
            state.save()
        return

    sess_filename = f"{session.date_prefix}-{session.short_id}.md"
    out_path = project_dir / "sessions" / sess_filename

    if verbose:
        print(f"[sagent] {session_path.name} → {out_path.relative_to(out_root)}")

    timeline_md = build_timeline(session)

    if no_llm:
        write_session_md(
            session,
            out_path,
            summary_md="(LLM digest skipped — `--no-llm`)\n",
            understanding_md="",
            timeline_md=timeline_md,
        )
        if state is not None:
            state.mark_digested(
                session_path, size=current_size, event_index=len(session.events)
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
    prior_summary = ""
    prior_understanding = ""
    if do_incremental:
        existing = _existing_session_md(project_dir, session.session_id)
        if existing and existing.exists():
            prior_text = existing.read_text()
            # Extract the previous Summary and Understanding sections
            prior_summary, prior_understanding = _extract_prior_sections(prior_text)
        if not prior_summary.strip():
            do_incremental = False

    try:
        if do_incremental:
            assert rec is not None
            new_count = len(session.events) - rec.last_event_index
            if verbose:
                print(
                    f"  … incremental ({new_count} new events, "
                    f"prior at index {rec.last_event_index})"
                )
            summary_md, understanding_md = run_understanding(
                session,
                model=model,
                prior_summary=prior_summary,
                prior_understanding=prior_understanding,
                since_event_index=rec.last_event_index,
                rate_limiter=rate_limiter,
            )
        else:
            if verbose:
                reason = (
                    "force-full" if force_full
                    else "rebuild cycle" if rec and (digest_count + 1) % full_rebuild_every == 0
                    else "cold start"
                )
                print(f"  … full digest ({reason})")
            summary_md, understanding_md = run_understanding(
                session, model=model, rate_limiter=rate_limiter
            )
    except SagentRateLimitError:
        # Don't mark state — let the next pass retry once cooldown lifts.
        raise
    except Exception as exc:
        print(f"[sagent] understanding failed for {session_path.name}: {exc}")
        return

    write_session_md(
        session,
        out_path,
        summary_md=summary_md,
        understanding_md=understanding_md,
        timeline_md=timeline_md,
    )

    if state is not None:
        state.mark_digested(
            session_path, size=current_size, event_index=len(session.events)
        )
        state.save()

    # Project-level roll-up (or scratchpad recent.md)
    if skip_rollup:
        return
    try:
        _maybe_rollup(
            project_dir=project_dir,
            new_session_path=out_path,
            session_id=session.session_id,
            model=model,
            state=state,
            force_full=force_full,
            full_rebuild_every=full_rebuild_every,
            rate_limiter=rate_limiter,
            verbose=verbose,
        )
    except SagentRateLimitError:
        raise
    except Exception as exc:
        print(f"[sagent] roll-up failed for {project_dir.name}: {exc}")


def _extract_prior_sections(session_md: str) -> tuple[str, str]:
    """Pull the '## Summary' and '## Understanding' bodies from a per-session
    combined digest."""
    import re

    def _section(name: str) -> str:
        m = re.search(rf"^## {name}\s*$\n+(.*?)(?=\n## |\Z)", session_md, re.M | re.S)
        return m.group(1).strip() if m else ""

    return _section("Summary"), _section("Understanding")


def _maybe_rollup(
    *,
    project_dir: Path,
    new_session_path: Path,
    session_id: str,
    model: str,
    state: StateStore | None,
    force_full: bool,
    full_rebuild_every: int,
    rate_limiter: RateLimiter | None,
    verbose: bool,
) -> None:
    if is_scratchpad(project_dir.name):
        update_recent(project_dir)
        if verbose:
            print(f"  ✓ updated {project_dir.name}/recent.md")
        return

    rollup_count = 0
    if state is not None:
        prec = state.get_project(project_dir.name)
        if prec:
            rollup_count = prec.rollup_count

    if verbose:
        print(f"  … rolling up {project_dir.name}/project.md")
    roll_up_project(
        project_dir,
        new_session_path=new_session_path,
        model=model,
        force_full=force_full,
        full_rebuild_every=full_rebuild_every,
        rollup_count=rollup_count,
        rate_limiter=rate_limiter,
    )
    if state is not None:
        state.mark_rolled_up(project_dir.name, session_id=session_id)
        state.save()


def _make_state(args: argparse.Namespace) -> StateStore | None:
    if getattr(args, "no_state", False):
        return None
    return StateStore(Path(args.state) if args.state else None)


def _make_rate_limiter(args: argparse.Namespace) -> RateLimiter | None:
    n = getattr(args, "max_per_hour", 0) or 0
    return RateLimiter(max_per_hour=n) if n > 0 else None


def cmd_digest(args: argparse.Namespace) -> int:
    session_path = _resolve_input(args.target)
    out_root = Path(args.out) if args.out else default_out_dir()
    state = _make_state(args)
    rate_limiter = _make_rate_limiter(args)
    try:
        _digest_one(
            session_path,
            out_root,
            model=args.model,
            no_llm=args.no_llm,
            state=state,
            force_full=args.force_full,
            full_rebuild_every=args.full_rebuild_every,
            min_prompts=args.min_prompts,
            skip_rollup=args.skip_rollup,
            rate_limiter=rate_limiter,
        )
    except SagentRateLimitError as exc:
        print(f"[sagent] rate limit hit: {exc}")
        return 2
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    out_root = Path(args.out) if args.out else default_out_dir()
    state = _make_state(args)
    rate_limiter = _make_rate_limiter(args)

    def on_change(path: Path) -> None:
        _digest_one(
            path,
            out_root,
            model=args.model,
            no_llm=args.no_llm,
            state=state,
            force_full=args.force_full,
            full_rebuild_every=args.full_rebuild_every,
            min_prompts=args.min_prompts,
            rate_limiter=rate_limiter,
        )

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
    rate_limiter = _make_rate_limiter(args)
    print(f"[sagent] output root: {out_root}")
    if state is not None:
        print(f"[sagent] state: {state.path}")
    if rate_limiter is not None:
        print(f"[sagent] rate limit: {args.max_per_hour}/hour")

    def on_change(path: Path) -> None:
        _digest_one(
            path,
            out_root,
            model=args.model,
            no_llm=args.no_llm,
            state=state,
            force_full=args.force_full,
            full_rebuild_every=args.full_rebuild_every,
            min_prompts=args.min_prompts,
            rate_limiter=rate_limiter,
        )

    watch_all(
        on_change,
        min_bytes=args.min_bytes,
        min_delta=args.min_delta,
        quiet_seconds=args.idle_seconds,
        state=state,
        rate_limit_cooldown=args.rate_limit_cooldown,
    )
    return 0


def cmd_digest_all(args: argparse.Namespace) -> int:
    out_root = Path(args.out) if args.out else default_out_dir()
    state = _make_state(args)
    rate_limiter = _make_rate_limiter(args)
    print(f"[sagent] output root: {out_root}")
    if state is not None:
        print(f"[sagent] state: {state.path}")
    count = 0
    skipped = 0
    # Real projects first, scratchpads last
    projs = [p for p in CLAUDE_PROJECTS.iterdir() if p.is_dir()]
    projs.sort(key=lambda p: (is_scratchpad(p.name), p.name))
    for proj in projs:
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
            try:
                _digest_one(
                    sess,
                    out_root,
                    model=args.model,
                    no_llm=args.no_llm,
                    state=state,
                    force_full=args.force_full,
                    full_rebuild_every=args.full_rebuild_every,
                    min_prompts=args.min_prompts,
                    rate_limiter=rate_limiter,
                )
            except SagentRateLimitError as exc:
                print(f"[sagent] rate limit hit, stopping: {exc}")
                break
            count += 1
    print(f"[sagent] digested {count}; skipped {skipped} already-digested")
    return 0


def cmd_rollup(args: argparse.Namespace) -> int:
    """Re-run the project-level roll-up against existing per-session digests.

    Useful after migration or to force-refresh a stale project.md.
    """
    out_root = Path(args.out) if args.out else default_out_dir()
    state = _make_state(args) if not args.no_state else None
    project_filter = args.project

    if not out_root.exists():
        sys.exit(f"no output at {out_root}")

    for project_dir in sorted(out_root.iterdir()):
        if not project_dir.is_dir():
            continue
        if project_filter and project_dir.name != project_filter:
            continue
        sessions_dir = project_dir / "sessions"
        if not sessions_dir.exists() or not any(sessions_dir.glob("*.md")):
            continue

        if is_scratchpad(project_dir.name):
            print(f"[sagent] {project_dir.name} (scratchpad) → recent.md")
            update_recent(project_dir)
            continue

        latest = max(sessions_dir.glob("*.md"), key=lambda p: p.stat().st_mtime)
        rollup_count = 0
        if state is not None:
            prec = state.get_project(project_dir.name)
            if prec:
                rollup_count = prec.rollup_count
        print(f"[sagent] {project_dir.name} → project.md (force_full={args.force_full})")
        roll_up_project(
            project_dir,
            new_session_path=latest,
            model=args.model,
            force_full=args.force_full,
            full_rebuild_every=args.full_rebuild_every,
            rollup_count=rollup_count,
        )
        if state is not None:
            # use the latest session's id8 as the marker
            import re

            m = re.match(r"^\d{4}-\d{2}-\d{2}-([0-9a-f]+)\.md$", latest.name)
            if m:
                state.mark_rolled_up(project_dir.name, session_id=m.group(1))
                state.save()

    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    """Remove per-session .md files whose source has too few user prompts.

    Walks <project>/sessions/*.md, derives the source UUID from the filename,
    re-parses the source JSONL, and drops the .md if user_prompts < min.
    """
    out_root = Path(args.out) if args.out else default_out_dir()
    state = _make_state(args) if not args.no_state else None

    if not out_root.exists():
        print(f"[sagent] nothing at {out_root}")
        return 0

    import re

    removed = 0
    kept = 0
    orphaned = 0
    for proj_dir in sorted(out_root.iterdir()):
        if not proj_dir.is_dir():
            continue
        sessions_dir = proj_dir / "sessions"
        if not sessions_dir.exists():
            continue
        for md in sorted(sessions_dir.glob("*.md")):
            m = re.match(r"^\d{4}-\d{2}-\d{2}-([0-9a-f]+)\.md$", md.name)
            if not m:
                continue
            short = m.group(1)
            matches = list(CLAUDE_PROJECTS.glob(f"*/{short}*.jsonl"))
            if not matches:
                orphaned += 1
                if args.prune_orphans:
                    if args.dry_run:
                        print(f"  [orphan] would remove {md.relative_to(out_root)}")
                    else:
                        md.unlink()
                        removed += 1
                continue
            source = matches[0]
            session = load_session(source)
            if len(session.user_prompts) < args.min_prompts:
                if args.dry_run:
                    print(
                        f"  would remove {md.relative_to(out_root)} "
                        f"({len(session.user_prompts)} prompts)"
                    )
                else:
                    md.unlink()
                    if state is not None:
                        state.mark_digested(
                            source,
                            size=source.stat().st_size,
                            event_index=len(session.events),
                        )
                removed += 1
            else:
                kept += 1
    if state is not None and not args.dry_run:
        state.save()
    verb = "would remove" if args.dry_run else "removed"
    print(
        f"[sagent] {verb} {removed}, kept {kept}, orphans {orphaned}"
        + (
            " (use --prune-orphans to remove those too)"
            if orphaned and not args.prune_orphans
            else ""
        )
    )
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
        kind = "scratchpad" if is_scratchpad(proj.name) else "project"
        print(f"{proj.name}  ({len(sessions)} sessions, {kind})")
        if args.verbose:
            for s in sessions[-3:]:
                print(f"  {s.name}  {s.stat().st_size:>10} bytes")
    return 0


def _add_min_prompts_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--min-prompts",
        type=int,
        default=1,
        help="drop sessions with fewer than this many user prompts (default: 1)",
    )


def _add_rate_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--max-per-hour",
        type=int,
        default=0,
        help=(
            "max LLM calls per rolling hour (default: 0 = unlimited). "
            "Counts every per-session digest AND every project rollup as one call."
        ),
    )


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
    pd.add_argument(
        "--skip-rollup",
        action="store_true",
        help="skip the project-level project.md / recent.md update",
    )
    _add_min_prompts_arg(pd)
    _add_rate_args(pd)
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
    _add_min_prompts_arg(pda)
    _add_rate_args(pda)
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
    _add_min_prompts_arg(pw)
    _add_rate_args(pw)
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
    pwa.add_argument(
        "--rate-limit-cooldown",
        type=float,
        default=1800.0,
        help=(
            "seconds to sleep when the API reports rate-limit before "
            "resuming digests (default: 1800)"
        ),
    )
    _add_min_prompts_arg(pwa)
    _add_rate_args(pwa)
    _add_state_args(pwa)
    pwa.set_defaults(func=cmd_watch_all)

    pru = sub.add_parser(
        "rollup",
        help="re-run project-level roll-up against existing per-session digests",
    )
    pru.add_argument(
        "project", nargs="?", help="project dir name (defaults to all projects)"
    )
    pru.add_argument("--out", default=None, help=out_help)
    pru.add_argument("--model", **common_model)
    _add_state_args(pru)
    pru.set_defaults(func=cmd_rollup)

    ppr = sub.add_parser(
        "prune", help="delete per-session digests whose source has no real content"
    )
    ppr.add_argument("--out", default=None, help=out_help)
    ppr.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would be removed without deleting",
    )
    ppr.add_argument(
        "--prune-orphans",
        action="store_true",
        help="also remove digests whose source JSONL no longer exists",
    )
    _add_min_prompts_arg(ppr)
    _add_state_args(ppr)
    ppr.set_defaults(func=cmd_prune)

    pl = sub.add_parser("list", help="list Claude Code projects with sessions")
    pl.add_argument("-v", "--verbose", action="store_true")
    pl.set_defaults(func=cmd_list)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
