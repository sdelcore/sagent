from __future__ import annotations

import argparse
import os
import socket
import sys
from collections import Counter
from pathlib import Path

from .parser import load_session
from .pipeline import (
    DigestConfig,
    DigestOutcome,
    clean_project_name,
    digest_session,
)
from .rate import RateLimiter, SagentRateLimitError
from .rollup import is_scratchpad, roll_up_project, update_recent
from .state import DigestLedger, NullLedger, default_state_path
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


def _make_ledger(args: argparse.Namespace) -> DigestLedger:
    """Build a DigestLedger or a NullLedger when --no-state is set.

    Always returns *some* ledger so the pipeline never branches on
    `ledger is None`.
    """
    if getattr(args, "no_state", False):
        return NullLedger()
    return DigestLedger(Path(args.state) if args.state else None)


def _make_rate_limiter(args: argparse.Namespace) -> RateLimiter | None:
    n = getattr(args, "max_per_hour", 0) or 0
    return RateLimiter(max_per_hour=n) if n > 0 else None


def _config_from(args: argparse.Namespace, *, out_root: Path) -> DigestConfig:
    """Build a DigestConfig from a parsed argparse Namespace.

    Tolerant of missing attrs (some subcommands don't expose every flag).
    """
    return DigestConfig(
        out_root=out_root,
        model=getattr(args, "model", "claude-haiku-4-5"),
        no_llm=getattr(args, "no_llm", False),
        force_full=getattr(args, "force_full", False),
        full_rebuild_every=getattr(args, "full_rebuild_every", 10),
        min_delta=getattr(args, "min_delta", 0),
        min_prompts=getattr(args, "min_prompts", 1),
        skip_rollup=getattr(args, "skip_rollup", False),
        verbose=True,
    )


def _print_ledger_path(ledger: DigestLedger) -> None:
    if isinstance(ledger, NullLedger):
        print("[sagent] state: --no-state (in-memory only)")
    else:
        print(f"[sagent] state: {ledger.path}")


def cmd_digest(args: argparse.Namespace) -> int:
    session_path = _resolve_input(args.target)
    out_root = Path(args.out) if args.out else default_out_dir()
    ledger = _make_ledger(args)
    rate_limiter = _make_rate_limiter(args)
    config = _config_from(args, out_root=out_root)
    try:
        digest_session(
            session_path,
            config,
            ledger=ledger,
            rate_limiter=rate_limiter,
        )
    except SagentRateLimitError as exc:
        print(f"[sagent] rate limit hit: {exc}")
        return 2
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    out_root = Path(args.out) if args.out else default_out_dir()
    ledger = _make_ledger(args)
    rate_limiter = _make_rate_limiter(args)
    config = _config_from(args, out_root=out_root)

    def on_change(path: Path) -> None:
        digest_session(path, config, ledger=ledger, rate_limiter=rate_limiter)

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
    ledger = _make_ledger(args)
    rate_limiter = _make_rate_limiter(args)
    config = _config_from(args, out_root=out_root)
    print(f"[sagent] output root: {out_root}")
    _print_ledger_path(ledger)
    if rate_limiter is not None:
        print(f"[sagent] rate limit: {args.max_per_hour}/hour")

    def on_change(path: Path) -> None:
        digest_session(path, config, ledger=ledger, rate_limiter=rate_limiter)

    watch_all(
        on_change,
        min_bytes=args.min_bytes,
        min_delta=args.min_delta,
        quiet_seconds=args.idle_seconds,
        ledger=ledger,
        rate_limit_cooldown=args.rate_limit_cooldown,
    )
    return 0


def cmd_digest_all(args: argparse.Namespace) -> int:
    out_root = Path(args.out) if args.out else default_out_dir()
    ledger = _make_ledger(args)
    rate_limiter = _make_rate_limiter(args)
    config = _config_from(args, out_root=out_root)
    print(f"[sagent] output root: {out_root}")
    _print_ledger_path(ledger)

    counts: Counter[str] = Counter()
    # Real projects first, scratchpads last
    projs = [p for p in CLAUDE_PROJECTS.iterdir() if p.is_dir()]
    projs.sort(key=lambda p: (is_scratchpad(p.name), p.name))
    rate_limited = False
    for proj in projs:
        if rate_limited:
            break
        for sess in sorted(proj.glob("*.jsonl")):
            try:
                size = sess.stat().st_size
            except FileNotFoundError:
                continue
            if size < args.min_bytes:
                continue
            try:
                outcome: DigestOutcome = digest_session(
                    sess, config, ledger=ledger, rate_limiter=rate_limiter
                )
            except SagentRateLimitError as exc:
                print(f"[sagent] rate limit hit, stopping: {exc}")
                rate_limited = True
                break
            counts[outcome.status] += 1

    print(
        f"[sagent] digested {counts['digested']}; "
        f"skipped {counts['skipped']}; "
        f"dropped {counts['dropped']}"
    )
    return 0


def cmd_rollup(args: argparse.Namespace) -> int:
    """Re-run the project-level roll-up against existing per-session digests.

    Useful after migration or to force-refresh a stale project.md.
    """
    out_root = Path(args.out) if args.out else default_out_dir()
    ledger = _make_ledger(args)
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
        rollup_claim = ledger.claim_rollup(project_dir.name)
        # Pull cwd from the latest session's front matter for source context
        from .frontmatter import split_front_matter

        fm, _ = split_front_matter(latest.read_text(errors="ignore"))
        cwd = fm.get("cwd")
        project_source_path = Path(cwd) if cwd else None
        print(f"[sagent] {project_dir.name} → project.md (force_full={args.force_full})")
        roll_up_project(
            project_dir,
            new_session_path=latest,
            project_source_path=project_source_path,
            model=args.model,
            force_full=args.force_full,
            full_rebuild_every=args.full_rebuild_every,
            rollup_count=rollup_claim.prior_count,
        )
        # Use the latest session's id8 as the rollup marker.
        import re

        m = re.match(r"^\d{4}-\d{2}-\d{2}-([0-9a-f]+)\.md$", latest.name)
        if m:
            rollup_claim.commit(session_id=m.group(1))

    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    """Remove per-session .md files whose source has too few user prompts.

    Walks <project>/sessions/*.md, derives the source UUID from the filename,
    re-parses the source JSONL, and drops the .md if user_prompts < min.
    """
    out_root = Path(args.out) if args.out else default_out_dir()
    ledger = _make_ledger(args)

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
                    ledger.mark_digested(
                        source,
                        size=source.stat().st_size,
                        event_index=len(session.events),
                    )
                removed += 1
            else:
                kept += 1
    if not args.dry_run:
        ledger.save()
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


def cmd_purge_self(args: argparse.Namespace) -> int:
    """Delete sagent-self-generated JSONL files from ~/.claude/projects/.

    Walks every project dir (or one named via --project), parses each JSONL,
    and deletes those whose first user prompt matches sagent's own headers
    (Session `…`, Project: `…`, PRIOR SUMMARY:, PRIOR PROJECT.md:). These
    are leftovers from before v0.7 added --no-session-persistence to the
    Agent SDK call.
    """
    if not CLAUDE_PROJECTS.exists():
        print(f"[sagent] no claude projects dir at {CLAUDE_PROJECTS}")
        return 0

    targets: list[Path] = []
    for proj in sorted(CLAUDE_PROJECTS.iterdir()):
        if not proj.is_dir():
            continue
        if args.project and proj.name != args.project:
            continue
        targets.extend(proj.glob("*.jsonl"))

    deleted = 0
    kept = 0
    error = 0
    for f in targets:
        try:
            s = load_session(f)
        except Exception as exc:
            error += 1
            if args.verbose:
                print(f"  parse error on {f.name}: {exc}")
            continue
        if s.is_sagent_self_generated:
            if args.dry_run:
                if args.verbose:
                    print(
                        f"  would delete {f.parent.name}/{f.name}"
                    )
            else:
                try:
                    f.unlink()
                except OSError as exc:
                    print(f"  failed to remove {f}: {exc}")
                    error += 1
                    continue
            deleted += 1
        else:
            kept += 1

    verb = "would delete" if args.dry_run else "deleted"
    print(f"[sagent] {verb} {deleted}, kept {kept}, errors {error}")
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

    pps = sub.add_parser(
        "purge-self",
        help="delete sagent-self-generated JSONL files from ~/.claude/projects/",
    )
    pps.add_argument(
        "--project", default=None, help="restrict to one project dir name"
    )
    pps.add_argument(
        "--dry-run", action="store_true", help="report without deleting"
    )
    pps.add_argument("-v", "--verbose", action="store_true")
    pps.set_defaults(func=cmd_purge_self)

    pl = sub.add_parser("list", help="list Claude Code projects with sessions")
    pl.add_argument("-v", "--verbose", action="store_true")
    pl.set_defaults(func=cmd_list)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
