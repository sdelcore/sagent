"""Per-session digest pipeline.

The unit of work every command exercises: load one session, decide whether
to skip / drop / digest it, run the LLM in cold or incremental mode, write
the per-session markdown, and (unless suppressed) update the project
rollup.

Two harnesses feed that unit of work. Claude Code sessions are JSONL files
under `~/.claude/projects/`; opencode sessions live in a SQLite database
and are exported by the `opencode` binary. Only four things differ between
them — the ledger key, the monotonic size behind the skip check, how the
transcript is loaded, and how a cwd becomes a project key — so
`SessionSource` holds those four and every entry point shares one body.

The CLI module owns argparse and config wiring; the watcher module owns
the polling loops. Both call `digest_session` or `digest_opencode_session`
here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

from . import opencode
from .frontmatter import split_front_matter
from .parser import Session, load_session, short_session_id
from .project_context import read_project_context
from .rate import RateLimiter, SagentRateLimitError
from .rebrand import detect_rebrands
from .rollup import (
    RollupRejected,
    is_scratchpad,
    roll_up_project,
    update_index,
    update_recent,
)
from .session_doc import write_session_md
from .state import DigestLedger, NullLedger
from .understand import run_understanding


DigestStatus = Literal[
    "digested",
    "skipped",
    "dropped",
    "rate_limited",
    "rollup_refused",
]
DigestMode = Literal["full", "incremental", "no_llm"]

CLAUDE_HARNESS = "claude-code"

# A session whose harness reports no working directory still needs a home,
# and silently writing into the output root would scatter loose files there.
UNKNOWN_PROJECT = "unknown"


@dataclass(frozen=True)
class DigestConfig:
    """Settings for one digest pass. Built once per CLI invocation."""

    out_root: Path
    model: str = "claude-haiku-4-5"
    no_llm: bool = False
    force_full: bool = False
    full_rebuild_every: int = 10
    min_delta: int = 0
    min_prompts: int = 1
    skip_rollup: bool = False
    verbose: bool = True


@dataclass(frozen=True)
class DigestOutcome:
    """What one digest pass did to one session.

    `rollup_refused` is the odd one: the session markdown is written and
    `out_path` points at it, but the ledger claim was left uncommitted on
    purpose, so the same session is digested again on the next pass. See
    the D13 handler in `_digest`.
    """

    status: DigestStatus
    session_path: Path | str
    out_path: Path | None = None
    mode: DigestMode | None = None
    reason: str | None = None
    new_events: int | None = None
    harness: str = CLAUDE_HARNESS


@dataclass(frozen=True)
class SessionSource:
    """One session to digest, with its harness-specific parts resolved.

    `key` is the ledger key: a JSONL path for Claude Code, the URI
    `opencode://<id>` for opencode. `size` is any byte count that grows
    with the session, which is all the skip check and the settle tracker
    need. `label` names the session in log lines. `load` produces the
    transcript, and `project_name` maps the loaded session onto the
    project directory it belongs to.
    """

    key: Path | str
    size: int
    label: str
    harness: str
    load: Callable[[], Session]
    project_name: Callable[[Session], str]


def clean_project_name(dir_name: str) -> str:
    """Strip the `-home-<user>-src-` prefix off a Claude Code project dir name."""
    home = str(Path.home()).replace("/", "-")
    if dir_name.startswith(home + "-"):
        return dir_name[len(home) + 1 :]
    return dir_name.lstrip("-")


def project_name_for_cwd(cwd: str | Path | None) -> str:
    """Project key for an absolute working directory.

    Claude Code names its project directory by replacing `/` with `-` in
    the cwd, and `clean_project_name` strips the home prefix off that. A
    harness that reports a plain cwd instead (opencode) goes through the
    same two steps, so both land on one key for one working tree and share
    one project.md. That collision is the point, not an accident.

    `opencode.project_key` applies the same mapping to a `SessionRow`.
    """
    if not cwd:
        return ""
    return clean_project_name(str(cwd).rstrip("/").replace("/", "-"))


def project_dir_for(session_path: Path, out_root: Path) -> Path:
    return out_root / clean_project_name(session_path.parent.name)


def _existing_session_md(project_dir: Path, session_id: str) -> Path | None:
    sessions_dir = project_dir / "sessions"
    if not sessions_dir.exists():
        return None
    short = short_session_id(session_id)
    matches = list(sessions_dir.glob(f"*-{short}.md"))
    return matches[0] if matches else None


def _extract_prior_sections(session_md: str) -> tuple[str, str]:
    def _section(name: str) -> str:
        m = re.search(rf"^## {name}\s*$\n+(.*?)(?=\n## |\Z)", session_md, re.M | re.S)
        return m.group(1).strip() if m else ""

    return _section("Summary"), _section("Understanding")


def _say(cfg: DigestConfig, msg: str) -> None:
    if cfg.verbose:
        print(msg)


def digest_session(
    session_path: Path,
    config: DigestConfig,
    *,
    ledger: DigestLedger | None = None,
    rate_limiter: RateLimiter | None = None,
) -> DigestOutcome:
    """Digest one Claude Code session JSONL.

    Returns a DigestOutcome describing what happened. Re-raises
    SagentRateLimitError so the watcher can apply its cooldown.

    `ledger` defaults to a NullLedger so the pipeline never branches on
    its presence; pass a real DigestLedger to persist state across runs.
    """
    session_path = Path(session_path)
    try:
        current_size = session_path.stat().st_size
    except FileNotFoundError:
        return DigestOutcome(
            status="dropped",
            session_path=session_path,
            reason="source file vanished",
        )

    project_name = clean_project_name(session_path.parent.name)
    source = SessionSource(
        key=session_path,
        size=current_size,
        label=session_path.name,
        harness=CLAUDE_HARNESS,
        load=lambda: load_session(session_path),
        project_name=lambda _session: project_name,
    )
    return _digest(source, config, ledger=ledger, rate_limiter=rate_limiter)


def digest_opencode_session(
    session_id: str,
    config: DigestConfig,
    *,
    db_path: Path | str | None = None,
    directory: str | None = None,
    size: int | None = None,
    ledger: DigestLedger | None = None,
    rate_limiter: RateLimiter | None = None,
) -> DigestOutcome:
    """Digest one opencode session, identified by its database id.

    `db_path`, `directory` and `size` are what the watcher already read
    out of the database; pass them to avoid re-querying. Without them the
    database is discovered and the byte total is recomputed here.

    Content comes from `opencode --pure export`, so an export failure
    propagates like a parse failure does for Claude Code: the claim stays
    uncommitted and the next pass retries.
    """
    db = Path(db_path) if db_path is not None else opencode.find_database()
    if db is None:
        return DigestOutcome(
            status="dropped",
            session_path=opencode.ledger_key(session_id),
            reason="no opencode database found",
            harness=opencode.HARNESS,
        )

    refusal = _top_level_refusal(db, session_id)
    if refusal is not None:
        return DigestOutcome(
            status="dropped",
            session_path=opencode.ledger_key(session_id),
            reason=refusal,
            harness=opencode.HARNESS,
        )

    if size is None:
        size = opencode.session_bytes(db, session_id)

    source = SessionSource(
        key=opencode.ledger_key(session_id),
        size=size,
        label=opencode.ledger_key(session_id),
        harness=opencode.HARNESS,
        load=lambda: opencode.load_session(session_id, db_path=db),
        project_name=lambda session: (
            project_name_for_cwd(session.cwd or directory) or UNKNOWN_PROJECT
        ),
    )
    return _digest(source, config, ledger=ledger, rate_limiter=rate_limiter)


def _top_level_refusal(db: Path | str, session_id: str) -> str | None:
    """Why `session_id` must not be digested, or None when it may be.

    D3: a subagent's full result is already inlined in its parent's `task`
    tool part, so digesting the child would duplicate it and split one
    effort over two documents. `list_sessions` already excludes children,
    so membership answers the question.

    This fails CLOSED. The discovery query's `parent_id IS NULL` filter
    reads the same database, so it is unavailable exactly when this check
    is, and `sagent digest --harness opencode <id>` never consults that
    filter at all — on that path this is the only guard. Refusing costs
    one retry on the next pass, because nothing was claimed yet; failing
    open costs a duplicate session document that no later pass removes.
    """
    try:
        rows = opencode.list_sessions(db)
    except Exception as exc:
        return f"opencode database unreadable, refusing to guess: {exc}"
    if any(row.session_id == session_id for row in rows):
        return None
    return "not a top-level opencode session"


def _digest(
    source: SessionSource,
    config: DigestConfig,
    *,
    ledger: DigestLedger | None = None,
    rate_limiter: RateLimiter | None = None,
) -> DigestOutcome:
    """Digest one session from any harness. See `digest_session`."""
    if ledger is None:
        ledger = NullLedger()

    claim = ledger.claim(
        source.key,
        size=source.size,
        min_delta=config.min_delta,
        force=config.force_full,
    )
    if claim is None:
        _say(config, f"[sagent] {source.label} already digested, skipping")
        return DigestOutcome(
            status="skipped",
            session_path=source.key,
            reason="already digested at current size",
            harness=source.harness,
        )

    session = source.load()
    project_name = source.project_name(session)
    proj_dir = config.out_root / project_name

    if session.is_sagent_self_generated:
        _say(
            config,
            f"[sagent] {source.label} is sagent-self-generated, skipping",
        )
        existing = _existing_session_md(proj_dir, session.session_id)
        if existing and existing.exists():
            existing.unlink()
        claim.commit(event_index=len(session.events))
        return DigestOutcome(
            status="dropped",
            session_path=source.key,
            reason="sagent-self-generated",
            harness=source.harness,
        )

    if len(session.user_prompts) < config.min_prompts:
        _say(
            config,
            f"[sagent] {source.label} has {len(session.user_prompts)} "
            f"user prompts (< {config.min_prompts}), dropping",
        )
        existing = _existing_session_md(proj_dir, session.session_id)
        if existing and existing.exists():
            existing.unlink()
        claim.commit(event_index=len(session.events))
        return DigestOutcome(
            status="dropped",
            session_path=source.key,
            reason=f"only {len(session.user_prompts)} user prompts",
            harness=source.harness,
        )

    sess_filename = f"{session.date_prefix}-{session.short_id}.md"
    out_path = proj_dir / "sessions" / sess_filename

    _say(
        config,
        f"[sagent] {source.label} → {out_path.relative_to(config.out_root)}",
    )

    if config.no_llm:
        write_session_md(
            session,
            out_path,
            summary_md="(LLM digest skipped — `--no-llm`)\n",
            understanding_md="",
            project=project_name,
            source=source.harness,
            harness=source.harness,
        )
        claim.commit(event_index=len(session.events))
        return DigestOutcome(
            status="digested",
            session_path=source.key,
            out_path=out_path,
            mode="no_llm",
            harness=source.harness,
        )

    rec = claim.prior
    digest_count = rec.digest_count if rec else 0

    do_incremental = (
        rec is not None
        and rec.last_event_index > 0
        and rec.last_event_index < len(session.events)
        and not config.force_full
        and (
            config.full_rebuild_every <= 0
            or (digest_count + 1) % config.full_rebuild_every != 0
        )
    )
    prior_summary = ""
    prior_understanding = ""
    if do_incremental:
        existing = _existing_session_md(proj_dir, session.session_id)
        if existing and existing.exists():
            prior_text = existing.read_text()
            prior_summary, prior_understanding = _extract_prior_sections(prior_text)
        if not prior_summary.strip():
            do_incremental = False

    new_events_count: int | None = None
    mode: DigestMode
    try:
        if do_incremental:
            assert rec is not None
            new_events_count = len(session.events) - rec.last_event_index
            _say(
                config,
                f"  … incremental ({new_events_count} new events, "
                f"prior at index {rec.last_event_index})",
            )
            summary_md, understanding_md = run_understanding(
                session,
                model=config.model,
                prior_summary=prior_summary,
                prior_understanding=prior_understanding,
                since_event_index=rec.last_event_index,
                rate_limiter=rate_limiter,
            )
            mode = "incremental"
        else:
            reason_for_full = (
                "force-full"
                if config.force_full
                else (
                    "rebuild cycle"
                    if rec
                    and config.full_rebuild_every > 0
                    and (digest_count + 1) % config.full_rebuild_every == 0
                    else "cold start"
                )
            )
            _say(config, f"  … full digest ({reason_for_full})")
            summary_md, understanding_md = run_understanding(
                session,
                model=config.model,
                rate_limiter=rate_limiter,
            )
            mode = "full"
    except SagentRateLimitError:
        # Don't commit the claim — leave state untouched so we retry next pass.
        raise
    except Exception as exc:
        print(f"[sagent] understanding failed for {source.label}: {exc}")
        return DigestOutcome(
            status="dropped",
            session_path=source.key,
            reason=f"understanding failed: {exc}",
            harness=source.harness,
        )

    write_session_md(
        session,
        out_path,
        summary_md=summary_md,
        understanding_md=understanding_md,
        project=project_name,
        source=source.harness,
        harness=source.harness,
    )

    if not config.skip_rollup:
        try:
            _maybe_rollup(
                project_dir=proj_dir,
                new_session_path=out_path,
                session_id=session.session_id,
                project_source_path=Path(session.cwd) if session.cwd else None,
                config=config,
                ledger=ledger,
                rate_limiter=rate_limiter,
            )
        except SagentRateLimitError:
            # The session digest is written and already paid for, so record
            # it. Only the roll-up waits for the next pass.
            claim.commit(event_index=len(session.events))
            raise
        except RollupRejected as exc:
            # D13: the model answered with something that is not a digest,
            # project.md survives untouched, and this session's content is
            # therefore not in it yet. Leaving the SESSION claim uncommitted
            # is what makes the retry real. The uncommitted rollup claim
            # alone schedules nothing — it only holds back `rollup_count` —
            # so committing here would make the next pass skip the session
            # as "already digested at current size" and the refused content
            # would never reach project.md without a manual `sagent rollup`.
            #
            # The price is one repeated LLM digest per retry, which the rate
            # limiter bounds. Losing the work is not recoverable; paying for
            # the digest twice is.
            print(f"[sagent] REFUSED roll-up for {proj_dir.name}: {exc}")
            print(
                f"[sagent] {source.label} stays unclaimed and retries "
                f"next pass (prior project.md kept)"
            )
            return DigestOutcome(
                status="rollup_refused",
                session_path=source.key,
                out_path=out_path,
                mode=mode,
                reason=str(exc),
                new_events=new_events_count,
                harness=source.harness,
            )
        except Exception as exc:
            # Any other roll-up failure is about project.md alone. The
            # session digest holds, so the claim commits below.
            print(f"[sagent] roll-up failed for {proj_dir.name}: {exc}")

    claim.commit(event_index=len(session.events))

    return DigestOutcome(
        status="digested",
        session_path=source.key,
        out_path=out_path,
        mode=mode,
        new_events=new_events_count,
        harness=source.harness,
    )


def _maybe_rollup(
    *,
    project_dir: Path,
    new_session_path: Path,
    session_id: str,
    project_source_path: Path | None,
    config: DigestConfig,
    ledger: DigestLedger,
    rate_limiter: RateLimiter | None,
) -> None:
    # GROUPS.md is meant to fall out of the normal digest flow rather than out
    # of an explicit flag, so the model travels with every index refresh. A
    # `--no-llm` pass must stay offline, and `update_index` gates the call on
    # its own age check, so a batch of sessions still buys at most one.
    groups_model = None if config.no_llm else config.model

    if is_scratchpad(project_dir.name):
        update_recent(project_dir)
        _say(config, f"  ✓ updated {project_dir.name}/recent.md")
        update_index(
            project_dir.parent,
            groups_model=groups_model,
            rate_limiter=rate_limiter,
        )
        return

    rollup_claim = ledger.claim_rollup(project_dir.name)

    if config.verbose:
        ctx_note = (
            f" (with source from {project_source_path})"
            if project_source_path and Path(project_source_path).exists()
            else ""
        )
        print(f"  … rolling up {project_dir.name}/project.md{ctx_note}")

    roll_up_project(
        project_dir,
        new_session_path=new_session_path,
        project_source_path=project_source_path,
        model=config.model,
        force_full=config.force_full,
        full_rebuild_every=config.full_rebuild_every,
        rollup_count=rollup_claim.prior_count,
        rate_limiter=rate_limiter,
    )
    update_index(
        project_dir.parent,
        groups_model=groups_model,
        rate_limiter=rate_limiter,
    )
    detect_rebrands(project_dir.parent)
    rollup_claim.commit(session_id=session_id)


# Re-exports so callers (cli, watcher, tests) have one import surface.
__all__ = [
    "CLAUDE_HARNESS",
    "DigestConfig",
    "DigestOutcome",
    "DigestStatus",
    "DigestMode",
    "SessionSource",
    "digest_session",
    "digest_opencode_session",
    "clean_project_name",
    "project_name_for_cwd",
    "project_dir_for",
    "read_project_context",
    "split_front_matter",
]
