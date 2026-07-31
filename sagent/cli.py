from __future__ import annotations

import argparse
import os
import socket
import sys
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from . import opencode
from .opencode import OpencodeError
from .parser import load_session
from .pipeline import (
    CLAUDE_HARNESS,
    DigestConfig,
    DigestOutcome,
    clean_project_name,
    digest_opencode_session,
    digest_session,
)
from .rate import RateLimiter, SagentRateLimitError
from .rebrand import detect_rebrands
from .rollup import (
    RollupRejected,
    is_scratchpad,
    roll_up_project,
    session_harness,
    update_index,
    update_recent,
)
from .state import DigestLedger, NullLedger, default_state_path
from .watcher import (
    CLAUDE_PROJECTS,
    DEFAULT_DB_POLL_SECONDS,
    DEFAULT_QUIET_SECONDS,
    OpencodeTarget,
    latest_session,
    project_dir_for_cwd,
    watch_all,
    watch_opencode,
    watch_project,
)

OPENCODE_HARNESS = opencode.HARNESS
ALL_HARNESSES = "all"
HARNESS_CHOICES = (CLAUDE_HARNESS, OPENCODE_HARNESS, ALL_HARNESSES)


def default_out_dir() -> Path:
    """Compute the default output root.

    Precedence: $SAGENT_OUT > ~/.sagent/<hostname>

    The default is deliberately a plain dot-directory, not a vault path.
    sagent used to sniff for ~/Obsidian and write there when it existed,
    which made the destination depend on whether an unrelated program had
    been installed -- the same command wrote to two different places on two
    machines. Anyone who wants the digests in a vault says so once, via
    $SAGENT_OUT, --out, or services.sagent.outDir.

    The <hostname> segment stays because the document model is keyed
    <host>/<project>: INDEX.md aggregates the projects under one root, so
    two machines sharing a root would merge their fleets into one index.
    """
    env = os.environ.get("SAGENT_OUT")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".sagent" / socket.gethostname()


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


def _harness(args: argparse.Namespace) -> str:
    """The chosen harness, defaulting to both for commands without the flag."""
    return getattr(args, "harness", ALL_HARNESSES)


def _wants(args: argparse.Namespace, harness: str) -> bool:
    chosen = _harness(args)
    return chosen in (ALL_HARNESSES, harness)


def _opencode_db_override(args: argparse.Namespace) -> Path | None:
    """The explicit --opencode-db, or None to let discovery run.

    The watch loops re-discover the database while it is missing, so an
    absent override is worth more to them than a path resolved once at
    startup: opencode can be installed without restarting the service.
    """
    value = getattr(args, "opencode_db", None)
    if not value:
        return None
    if value == ":memory:" or value.startswith("file:"):
        return Path(value)
    return Path(value).expanduser()


def _opencode_db(args: argparse.Namespace) -> Path | None:
    """The database a one-shot command should read, or None when absent."""
    return _opencode_db_override(args) or opencode.find_database()


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


def _opencode_target_id(args: argparse.Namespace) -> str | None:
    """The opencode session id in `digest`'s target, or None for a file.

    `--harness opencode` reads the target verbatim as a session id. Under
    the default `all` sagent never guesses: the target is an opencode id
    only when no such path exists and the database really holds that id.
    """
    harness = _harness(args)
    if harness == CLAUDE_HARNESS:
        return None
    target = args.target
    if harness == OPENCODE_HARNESS:
        if not target:
            sys.exit("digest --harness opencode needs an opencode session id")
        return target
    if not target or Path(target).expanduser().exists():
        return None
    db = _opencode_db(args)
    if db is None:
        return None
    try:
        rows = opencode.list_sessions(db)
    except Exception:
        return None
    return target if any(row.session_id == target for row in rows) else None


def cmd_digest(args: argparse.Namespace) -> int:
    session_id = _opencode_target_id(args)
    out_root = Path(args.out) if args.out else default_out_dir()
    ledger = _make_ledger(args)
    rate_limiter = _make_rate_limiter(args)
    config = _config_from(args, out_root=out_root)

    if session_id is not None:
        db = _opencode_db(args)
        if db is None:
            sys.exit(
                "no opencode database found "
                "(set --opencode-db or $OPENCODE_DB)"
            )
        try:
            outcome = digest_opencode_session(
                session_id,
                config,
                db_path=db,
                ledger=ledger,
                rate_limiter=rate_limiter,
            )
        except SagentRateLimitError as exc:
            print(f"[sagent] rate limit hit: {exc}")
            return 2
        except OpencodeError as exc:
            print(f"[sagent] opencode: {exc}")
            return 2
        if outcome.status == "dropped":
            print(f"[sagent] {session_id} dropped: {outcome.reason}")
        return _digest_exit_code(outcome)

    session_path = _resolve_input(args.target)
    try:
        outcome = digest_session(
            session_path,
            config,
            ledger=ledger,
            rate_limiter=rate_limiter,
        )
    except SagentRateLimitError as exc:
        print(f"[sagent] rate limit hit: {exc}")
        return 2
    return _digest_exit_code(outcome)


def _digest_exit_code(outcome: DigestOutcome) -> int:
    """0, or 1 when the roll-up refused the model's answer.

    A refusal keeps the prior project.md and leaves the session unclaimed
    for the next pass, so nothing is lost — but the exit code says so, the
    same way `sagent rollup` does.
    """
    if outcome.status == "rollup_refused":
        return 1
    return 0


def _opencode_callback(
    config: DigestConfig,
    *,
    ledger: DigestLedger,
    rate_limiter: RateLimiter | None,
) -> Callable[[OpencodeTarget], bool]:
    """Adapt a settled OpencodeTarget onto the opencode digest.

    The target carries everything the database sweep already read, so the
    digest re-queries nothing.

    Returns False when the roll-up was refused, so the watch loop leaves the
    session unfired and tries again. Reporting `True` there would strand the
    session: it has settled, so its size never changes again.
    """

    def on_opencode(target: OpencodeTarget) -> bool:
        outcome = digest_opencode_session(
            target.session_id,
            config,
            db_path=target.db_path,
            directory=target.row.directory,
            size=target.size,
            ledger=ledger,
            rate_limiter=rate_limiter,
        )
        return outcome.status != "rollup_refused"

    return on_opencode


def cmd_watch(args: argparse.Namespace) -> int:
    out_root = Path(args.out) if args.out else default_out_dir()
    ledger = _make_ledger(args)
    rate_limiter = _make_rate_limiter(args)
    config = _config_from(args, out_root=out_root)

    if _harness(args) == OPENCODE_HARNESS:
        if args.target:
            sys.exit(
                "watch --harness opencode takes no target: opencode keeps "
                "every session in one database, so the whole database is watched"
            )
        watch_opencode(
            _opencode_callback(config, ledger=ledger, rate_limiter=rate_limiter),
            db_path=_opencode_db_override(args),
            quiet_seconds=args.idle_seconds,
            ledger=ledger,
        )
        return 0

    def on_change(path: Path) -> bool:
        outcome = digest_session(
            path, config, ledger=ledger, rate_limiter=rate_limiter
        )
        return outcome.status != "rollup_refused"

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

    def on_change(path: Path) -> bool:
        outcome = digest_session(
            path, config, ledger=ledger, rate_limiter=rate_limiter
        )
        return outcome.status != "rollup_refused"

    on_opencode = (
        _opencode_callback(config, ledger=ledger, rate_limiter=rate_limiter)
        if _wants(args, OPENCODE_HARNESS)
        else None
    )

    if not _wants(args, CLAUDE_HARNESS):
        watch_opencode(
            on_opencode,
            db_path=_opencode_db_override(args),
            interval=args.db_poll_seconds,
            quiet_seconds=args.idle_seconds,
            min_bytes=args.min_bytes,
            min_delta=args.min_delta,
            ledger=ledger,
            rate_limit_cooldown=args.rate_limit_cooldown,
        )
        return 0

    watch_all(
        on_change,
        min_bytes=args.min_bytes,
        min_delta=args.min_delta,
        quiet_seconds=args.idle_seconds,
        ledger=ledger,
        rate_limit_cooldown=args.rate_limit_cooldown,
        on_opencode=on_opencode,
        db_path=_opencode_db_override(args),
        db_poll_seconds=args.db_poll_seconds,
    )
    return 0


def _digest_all_claude(
    args: argparse.Namespace,
    config: DigestConfig,
    *,
    ledger: DigestLedger,
    rate_limiter: RateLimiter | None,
    counts: Counter[str],
) -> bool:
    """Digest every Claude Code session. True means a rate limit stopped it."""
    if not CLAUDE_PROJECTS.exists():
        print(f"[sagent] no claude projects dir at {CLAUDE_PROJECTS}, skipping")
        return False
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
            try:
                outcome: DigestOutcome = digest_session(
                    sess, config, ledger=ledger, rate_limiter=rate_limiter
                )
            except SagentRateLimitError as exc:
                print(f"[sagent] rate limit hit, stopping: {exc}")
                return True
            counts[outcome.status] += 1
    return False


def _digest_all_opencode(
    args: argparse.Namespace,
    config: DigestConfig,
    *,
    ledger: DigestLedger,
    rate_limiter: RateLimiter | None,
    counts: Counter[str],
) -> bool:
    """Digest every top-level opencode session. True means a rate limit hit.

    An export failure is per-session, not fatal: the claim stays uncommitted,
    so the session is retried on the next run.
    """
    db = _opencode_db(args)
    if db is None:
        print("[sagent] no opencode database found, skipping")
        return False
    try:
        rows = opencode.list_sessions(db)
    except Exception as exc:
        print(f"[sagent] cannot read {db}: {exc}")
        return False
    print(f"[sagent] opencode: {len(rows)} top-level session(s) in {db}")
    for row in rows:
        size = opencode.session_bytes(db, row.session_id)
        if size < args.min_bytes:
            continue
        try:
            outcome = digest_opencode_session(
                row.session_id,
                config,
                db_path=db,
                directory=row.directory,
                size=size,
                ledger=ledger,
                rate_limiter=rate_limiter,
            )
        except SagentRateLimitError as exc:
            print(f"[sagent] rate limit hit, stopping: {exc}")
            return True
        except OpencodeError as exc:
            print(f"[sagent] opencode {row.session_id}: {exc}")
            counts["error"] += 1
            continue
        counts[outcome.status] += 1
    return False


def cmd_digest_all(args: argparse.Namespace) -> int:
    out_root = Path(args.out) if args.out else default_out_dir()
    ledger = _make_ledger(args)
    rate_limiter = _make_rate_limiter(args)
    config = _config_from(args, out_root=out_root)
    print(f"[sagent] output root: {out_root}")
    _print_ledger_path(ledger)

    counts: Counter[str] = Counter()
    rate_limited = False
    if _wants(args, CLAUDE_HARNESS):
        rate_limited = _digest_all_claude(
            args, config, ledger=ledger, rate_limiter=rate_limiter, counts=counts
        )
    if _wants(args, OPENCODE_HARNESS) and not rate_limited:
        _digest_all_opencode(
            args, config, ledger=ledger, rate_limiter=rate_limiter, counts=counts
        )

    print(
        f"[sagent] digested {counts['digested']}; "
        f"skipped {counts['skipped']}; "
        f"dropped {counts['dropped']}"
    )
    if counts["error"]:
        print(f"[sagent] {counts['error']} session(s) failed to export")
    if counts["rollup_refused"]:
        # The session markdown for these is on disk, but the roll-up refused
        # the model's answer, so project.md does not have them yet and the
        # sessions are deliberately unclaimed. Say so, or the operator reads
        # "digested 0" and assumes there was no work.
        print(
            f"[sagent] {counts['rollup_refused']} session(s) had their "
            f"roll-up refused; prior project.md kept, retrying next pass"
        )
        return 1
    return 0


def cmd_rollup(args: argparse.Namespace) -> int:
    """Re-run the project-level roll-up against existing per-session digests.

    Useful after migration or to force-refresh a stale project.md.
    """
    out_root = Path(args.out) if args.out else default_out_dir()
    ledger = _make_ledger(args)
    rate_limiter = _make_rate_limiter(args)
    project_filter = args.project
    refused: list[str] = []

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
        try:
            roll_up_project(
                project_dir,
                new_session_path=latest,
                project_source_path=project_source_path,
                model=args.model,
                force_full=args.force_full,
                full_rebuild_every=args.full_rebuild_every,
                rollup_count=rollup_claim.prior_count,
                rate_limiter=rate_limiter,
            )
        except RollupRejected as exc:
            # The model answered with something that is not a digest. The
            # prior project.md survives, the claim stays uncommitted, and the
            # next pass retries — but the user must hear about it.
            print(f"[sagent] REFUSED {project_dir.name}: {exc}")
            refused.append(project_dir.name)
            continue
        # Use the latest session's id8 as the rollup marker.
        import re

        m = re.match(r"^\d{4}-\d{2}-\d{2}-([0-9a-f]+)\.md$", latest.name)
        if m:
            rollup_claim.commit(session_id=m.group(1))

    for key, flag in detect_rebrands(out_root):
        if flag:
            print(f"[sagent] rebrand detected: {key} → {flag}")

    index = update_index(
        out_root,
        groups_model=args.model if args.groups else None,
        # `--groups` is a request, not a hint: bypass the age gate that keeps
        # the automatic digest flow from buying a call per session.
        groups_max_age_hours=0.0,
        rate_limiter=rate_limiter,
    )
    if index is not None:
        print(f"[sagent] wrote {index}")

    if refused:
        print(
            f"[sagent] {len(refused)} roll-up(s) refused, prior project.md kept: "
            + ", ".join(sorted(refused))
        )
        return 1
    return 0


def cmd_index(args: argparse.Namespace) -> int:
    """Rebuild INDEX.md, and the advisory GROUPS.md with --groups."""
    out_root = Path(args.out) if args.out else default_out_dir()
    if not out_root.exists():
        sys.exit(f"no output at {out_root}")
    rate_limiter = _make_rate_limiter(args)
    index = update_index(
        out_root,
        groups_model=args.model if args.groups else None,
        # `--groups` is a request, not a hint: bypass the age gate that keeps
        # the automatic digest flow from buying a call per session.
        groups_max_age_hours=0.0,
        rate_limiter=rate_limiter,
    )
    if index is None:
        print(f"[sagent] nothing to index at {out_root}")
        return 0
    print(f"[sagent] wrote {index}")
    groups = out_root / "GROUPS.md"
    if args.groups and groups.exists():
        print(f"[sagent] wrote {groups} (advisory)")
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
    foreign = 0
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
            # An opencode id8 is hex too, so the filename alone cannot say
            # which harness wrote this digest. Only Claude Code digests have
            # a JSONL behind them; anything else would look like an orphan
            # and be deleted.
            if session_harness(md.read_text(errors="ignore")) != CLAUDE_HARNESS:
                foreign += 1
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
    if foreign:
        print(f"[sagent] left {foreign} non-claude-code digest(s) untouched")
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


def _list_claude(args: argparse.Namespace) -> None:
    root = CLAUDE_PROJECTS
    print(f"[{CLAUDE_HARNESS}] {root}")
    if not root.exists():
        if _harness(args) == CLAUDE_HARNESS:
            sys.exit(f"no claude projects dir at {root}")
        print("  (not present)")
        return
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


def _list_opencode(args: argparse.Namespace) -> None:
    """List opencode sessions grouped by the cwd that keys their project.

    Child sessions never appear: `list_sessions` excludes them, and sagent
    never digests one.
    """
    db = _opencode_db(args)
    print(f"[{OPENCODE_HARNESS}] {db or '(no database found)'}")
    if db is None:
        if _harness(args) == OPENCODE_HARNESS:
            sys.exit("no opencode database found (set --opencode-db or $OPENCODE_DB)")
        return
    try:
        rows = opencode.list_sessions(db)
    except Exception as exc:
        print(f"  (cannot read {db}: {exc})")
        return
    by_project: dict[str, list[opencode.SessionRow]] = {}
    for row in rows:
        by_project.setdefault(row.project_key, []).append(row)
    for key in sorted(by_project):
        sessions = sorted(by_project[key], key=lambda r: r.time_updated)
        kind = "scratchpad" if is_scratchpad(key) else "project"
        print(f"{key}  ({len(sessions)} sessions, {kind})")
        if args.verbose:
            for row in sessions[-3:]:
                size = opencode.session_bytes(db, row.session_id)
                updated = opencode.to_iso(row.time_updated) or "?"
                print(f"  {row.ledger_key}  {size:>10} bytes  {updated}")


def cmd_list(args: argparse.Namespace) -> int:
    if _wants(args, CLAUDE_HARNESS):
        _list_claude(args)
    if _wants(args, OPENCODE_HARNESS):
        _list_opencode(args)
    return 0


def _add_min_prompts_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--min-prompts",
        type=int,
        default=1,
        help="drop sessions with fewer than this many user prompts (default: 1)",
    )


def _add_harness_args(
    p: argparse.ArgumentParser,
    *,
    default: str = ALL_HARNESSES,
    choices: tuple[str, ...] = HARNESS_CHOICES,
) -> None:
    """Add --harness and --opencode-db.

    Both harnesses share one ledger, one output root and one API budget, so
    `all` is the default wherever a command sweeps rather than targets.
    """
    p.add_argument(
        "--harness",
        choices=list(choices),
        default=default,
        help=f"which harness to read (default: {default})",
    )
    p.add_argument(
        "--opencode-db",
        default=None,
        metavar="PATH",
        help=(
            "opencode SQLite database (default: $OPENCODE_DB, else "
            "opencode*.db under $XDG_DATA_HOME/opencode, preferring the "
            "default channel's opencode.db then the first name in sort "
            "order — NOT the most recently written one, so pass this "
            "after an install-channel switch)"
        ),
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
    out_help = "output root (default: $SAGENT_OUT or ~/.sagent/<hostname>/)"

    pd = sub.add_parser("digest", help="digest a single session")
    pd.add_argument(
        "target",
        nargs="?",
        help=(
            "path to .jsonl, project dir, or cwd; an opencode session id also "
            "works; default = current cwd's latest"
        ),
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
    _add_harness_args(pd)
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
    _add_harness_args(pda)
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
    # `all` is meaningless here: a target names one Claude Code project, and
    # opencode has no per-project file to watch.
    _add_harness_args(
        pw, default=CLAUDE_HARNESS, choices=(CLAUDE_HARNESS, OPENCODE_HARNESS)
    )
    _add_rate_args(pw)
    _add_state_args(pw)
    pw.set_defaults(func=cmd_watch)

    pwa = sub.add_parser(
        "watch-all",
        help="watch every Claude Code project and every opencode session",
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
    pwa.add_argument(
        "--db-poll-seconds",
        type=float,
        default=DEFAULT_DB_POLL_SECONDS,
        help=(
            "how often to sweep the opencode database "
            f"(default: {DEFAULT_DB_POLL_SECONDS:.0f})"
        ),
    )
    _add_min_prompts_arg(pwa)
    _add_harness_args(pwa)
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
    pru.add_argument(
        "--groups",
        action="store_true",
        help="force a refresh of the advisory GROUPS.md now (one extra LLM call)",
    )
    _add_rate_args(pru)
    _add_state_args(pru)
    pru.set_defaults(func=cmd_rollup)

    pix = sub.add_parser("index", help="rebuild INDEX.md across every project")
    pix.add_argument("--out", default=None, help=out_help)
    pix.add_argument("--model", **common_model)
    pix.add_argument(
        "--groups",
        action="store_true",
        help="force a refresh of the advisory GROUPS.md now (one LLM call)",
    )
    _add_rate_args(pix)
    pix.set_defaults(func=cmd_index)

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

    pl = sub.add_parser("list", help="list projects with sessions, per harness")
    pl.add_argument("-v", "--verbose", action="store_true")
    _add_harness_args(pl)
    pl.set_defaults(func=cmd_list)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
