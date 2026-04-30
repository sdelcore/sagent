"""Per-session digest pipeline.

The unit of work every command exercises: load a session JSONL, decide
whether to skip / drop / digest it, run the LLM in cold or incremental
mode, write the per-session markdown, and (unless suppressed) update the
project rollup.

The CLI module owns argparse and config wiring; the watcher module owns
the polling loop. Both call `digest_session` here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .frontmatter import split_front_matter
from .parser import load_session
from .project_context import read_project_context
from .rate import RateLimiter, SagentRateLimitError
from .rollup import (
    is_scratchpad,
    roll_up_project,
    update_index,
    update_recent,
)
from .session_doc import write_session_md
from .state import DigestLedger, NullLedger
from .understand import run_understanding


DigestStatus = Literal["digested", "skipped", "dropped", "rate_limited"]
DigestMode = Literal["full", "incremental", "no_llm"]


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
    status: DigestStatus
    session_path: Path
    out_path: Path | None = None
    mode: DigestMode | None = None
    reason: str | None = None
    new_events: int | None = None


def clean_project_name(dir_name: str) -> str:
    """Strip the `-home-<user>-src-` prefix off a Claude Code project dir name."""
    home = str(Path.home()).replace("/", "-")
    if dir_name.startswith(home + "-"):
        return dir_name[len(home) + 1 :]
    return dir_name.lstrip("-")


def project_dir_for(session_path: Path, out_root: Path) -> Path:
    return out_root / clean_project_name(session_path.parent.name)


def _existing_session_md(project_dir: Path, session_id: str) -> Path | None:
    sessions_dir = project_dir / "sessions"
    if not sessions_dir.exists():
        return None
    short = session_id.split("-")[0][:8]
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
    """Digest one session JSONL.

    Returns a DigestOutcome describing what happened. Re-raises
    SagentRateLimitError so the watcher can apply its cooldown.

    `ledger` defaults to a NullLedger so the pipeline never branches on
    its presence; pass a real DigestLedger to persist state across runs.
    """
    if ledger is None:
        ledger = NullLedger()

    try:
        current_size = session_path.stat().st_size
    except FileNotFoundError:
        return DigestOutcome(
            status="dropped",
            session_path=session_path,
            reason="source file vanished",
        )

    claim = ledger.claim(
        session_path,
        size=current_size,
        min_delta=config.min_delta,
        force=config.force_full,
    )
    if claim is None:
        _say(config, f"[sagent] {session_path.name} already digested, skipping")
        return DigestOutcome(
            status="skipped",
            session_path=session_path,
            reason="already digested at current size",
        )

    session = load_session(session_path)
    proj_dir = project_dir_for(session_path, config.out_root)

    if session.is_sagent_self_generated:
        _say(
            config,
            f"[sagent] {session_path.name} is sagent-self-generated, skipping",
        )
        existing = _existing_session_md(proj_dir, session.session_id)
        if existing and existing.exists():
            existing.unlink()
        claim.commit(event_index=len(session.events))
        return DigestOutcome(
            status="dropped",
            session_path=session_path,
            reason="sagent-self-generated",
        )

    if len(session.user_prompts) < config.min_prompts:
        _say(
            config,
            f"[sagent] {session_path.name} has {len(session.user_prompts)} "
            f"user prompts (< {config.min_prompts}), dropping",
        )
        existing = _existing_session_md(proj_dir, session.session_id)
        if existing and existing.exists():
            existing.unlink()
        claim.commit(event_index=len(session.events))
        return DigestOutcome(
            status="dropped",
            session_path=session_path,
            reason=f"only {len(session.user_prompts)} user prompts",
        )

    sess_filename = f"{session.date_prefix}-{session.short_id}.md"
    out_path = proj_dir / "sessions" / sess_filename
    project_name = clean_project_name(session_path.parent.name)

    _say(
        config,
        f"[sagent] {session_path.name} → {out_path.relative_to(config.out_root)}",
    )

    if config.no_llm:
        write_session_md(
            session,
            out_path,
            summary_md="(LLM digest skipped — `--no-llm`)\n",
            understanding_md="",
            project=project_name,
        )
        claim.commit(event_index=len(session.events))
        return DigestOutcome(
            status="digested",
            session_path=session_path,
            out_path=out_path,
            mode="no_llm",
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
        print(f"[sagent] understanding failed for {session_path.name}: {exc}")
        return DigestOutcome(
            status="dropped",
            session_path=session_path,
            reason=f"understanding failed: {exc}",
        )

    write_session_md(
        session,
        out_path,
        summary_md=summary_md,
        understanding_md=understanding_md,
        project=project_name,
    )

    claim.commit(event_index=len(session.events))

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
            raise
        except Exception as exc:
            print(f"[sagent] roll-up failed for {proj_dir.name}: {exc}")

    return DigestOutcome(
        status="digested",
        session_path=session_path,
        out_path=out_path,
        mode=mode,
        new_events=new_events_count,
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
    if is_scratchpad(project_dir.name):
        update_recent(project_dir)
        _say(config, f"  ✓ updated {project_dir.name}/recent.md")
        update_index(project_dir.parent)
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
    update_index(project_dir.parent)
    rollup_claim.commit(session_id=session_id)


# Re-exports so callers (cli, watcher, tests) have one import surface.
__all__ = [
    "DigestConfig",
    "DigestOutcome",
    "DigestStatus",
    "DigestMode",
    "digest_session",
    "clean_project_name",
    "project_dir_for",
    "read_project_context",
    "split_front_matter",
]
