from __future__ import annotations

import os
import time
from pathlib import Path

from sagent.project_doc import (
    ProjectDoc,
    build_headline_block,
    diff_front_matter,
    momentum_bucket,
)
from sagent.project_doc import (
    _count_section_bullets,
    _days_since_last_session,
    _extract_description_tagline,
)


# ---------------------------------------------------------------------------
# DESCRIPTION/TAGLINE preamble
# ---------------------------------------------------------------------------


def test_extract_description_tagline_both_present():
    body = (
        "DESCRIPTION: A project that does X.\n"
        "TAGLINE: Currently doing Y.\n"
        "\n"
        "# my project\n"
        "\n"
        "## Current state\n"
        "Going well.\n"
    )
    desc, tag, rest = _extract_description_tagline(body)
    assert desc == "A project that does X."
    assert tag == "Currently doing Y."
    assert rest.startswith("# my project")


def test_extract_description_tagline_caps_at_280():
    long_desc = "x" * 500
    body = f"DESCRIPTION: {long_desc}\nTAGLINE: short\n\n# proj\n"
    desc, _, _ = _extract_description_tagline(body)
    assert len(desc) <= 280


def test_extract_description_tagline_missing_lines_tolerated():
    body = "# heading\nbody\n"
    desc, tag, rest = _extract_description_tagline(body)
    assert desc == ""
    assert tag == ""
    assert rest.startswith("# heading")


# ---------------------------------------------------------------------------
# Section bullet counts
# ---------------------------------------------------------------------------


def test_count_section_bullets():
    body = (
        "## Long-term decisions\n"
        "- a\n"
        "- b\n"
        "- c\n"
        "\n"
        "## Open threads\n"
        "- one\n"
        "\n"
        "## Risks & known issues\n"
    )
    counts = _count_section_bullets(body)
    assert counts["Long-term decisions"] == 3
    assert counts["Open threads"] == 1
    assert counts["Risks & known issues"] == 0


# ---------------------------------------------------------------------------
# Momentum + days_since_last_session
# ---------------------------------------------------------------------------


def _make_session(sessions_dir: Path, name: str, mtime: float) -> Path:
    p = sessions_dir / name
    p.write_text("## Summary\n\nFake.\n")
    os.utime(p, (mtime, mtime))
    return p


def test_momentum_bucket_cold_when_no_recent_activity():
    assert momentum_bucket(0, 0) == "cold"
    assert momentum_bucket(0, 5) == "cold"


def test_momentum_bucket_cooling():
    assert momentum_bucket(2, 5) == "cooling"
    assert momentum_bucket(1, 2) == "cooling"


def test_momentum_bucket_steady():
    assert momentum_bucket(3, 3) == "steady"
    assert momentum_bucket(1, 1) == "steady"


def test_momentum_bucket_rising():
    assert momentum_bucket(5, 2) == "rising"
    assert momentum_bucket(1, 0) == "rising"


def test_days_since_last_session_empty():
    assert _days_since_last_session([], now=time.time()) is None


def test_days_since_last_session_basic(tmp_path: Path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    now = time.time()
    f1 = _make_session(sessions, "2026-04-20-aaaaaaaa.md", now - 5 * 86_400)
    f2 = _make_session(sessions, "2026-04-23-bbbbbbbb.md", now - 2 * 86_400)
    assert _days_since_last_session([f1, f2], now=now) == 2


def test_days_since_last_session_just_now_is_zero(tmp_path: Path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    now = time.time()
    f = _make_session(sessions, "2026-04-25-cccccccc.md", now - 60)
    assert _days_since_last_session([f], now=now) == 0


# ---------------------------------------------------------------------------
# ProjectDoc.derive_front_matter
# ---------------------------------------------------------------------------


def _doc(name="proj", desc="d", tag="t", body="") -> ProjectDoc:
    return ProjectDoc(name=name, description=desc, tagline=tag, body=body)


def test_derive_front_matter_cold_when_empty(tmp_path: Path):
    fm = _doc().derive_front_matter(sessions_dir=tmp_path / "sessions")
    assert fm["session_count"] == 0
    assert fm["sessions_last_7d"] == 0
    assert fm["days_since_last_session"] is None
    assert fm["momentum"] == "cold"


def test_derive_front_matter_rising(tmp_path: Path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    now = time.time()
    _make_session(sessions, "2026-04-22-aaaaaaaa.md", now - 1 * 86_400)
    _make_session(sessions, "2026-04-22-bbbbbbbb.md", now - 3 * 86_400)
    _make_session(sessions, "2026-04-22-cccccccc.md", now - 6 * 86_400)
    _make_session(sessions, "2026-04-15-dddddddd.md", now - 10 * 86_400)
    fm = _doc().derive_front_matter(sessions_dir=sessions, now=now)
    assert fm["sessions_last_7d"] == 3
    assert fm["momentum"] == "rising"
    assert fm["days_since_last_session"] == 1


def test_derive_front_matter_steady(tmp_path: Path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    now = time.time()
    _make_session(sessions, "s1.md", now - 1 * 86_400)
    _make_session(sessions, "s2.md", now - 4 * 86_400)
    _make_session(sessions, "s3.md", now - 9 * 86_400)
    _make_session(sessions, "s4.md", now - 12 * 86_400)
    fm = _doc().derive_front_matter(sessions_dir=sessions, now=now)
    assert fm["sessions_last_7d"] == 2
    assert fm["momentum"] == "steady"


def test_derive_front_matter_cooling(tmp_path: Path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    now = time.time()
    _make_session(sessions, "s1.md", now - 2 * 86_400)
    _make_session(sessions, "s2.md", now - 8 * 86_400)
    _make_session(sessions, "s3.md", now - 10 * 86_400)
    _make_session(sessions, "s4.md", now - 13 * 86_400)
    fm = _doc().derive_front_matter(sessions_dir=sessions, now=now)
    assert fm["sessions_last_7d"] == 1
    assert fm["momentum"] == "cooling"


def test_derive_front_matter_cold_with_old_sessions(tmp_path: Path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    now = time.time()
    _make_session(sessions, "s1.md", now - 30 * 86_400)
    _make_session(sessions, "s2.md", now - 45 * 86_400)
    fm = _doc().derive_front_matter(sessions_dir=sessions, now=now)
    assert fm["sessions_last_7d"] == 0
    assert fm["momentum"] == "cold"
    assert fm["days_since_last_session"] == 30


def test_derive_front_matter_counts_section_bullets():
    body = (
        "## Long-term decisions\n- a\n- b\n"
        "## Open threads\n- one\n"
        "## User preferences\n- p\n- q\n- r\n"
        "## Risks & known issues\n- r1\n"
    )
    fm = _doc(body=body).derive_front_matter(sessions_dir=Path("/nonexistent"))
    assert fm["decisions"] == 2
    assert fm["open_threads"] == 1
    assert fm["preferences"] == 3
    assert fm["risks"] == 1


# ---------------------------------------------------------------------------
# Parse + render_body — round-trip and headline behavior
# ---------------------------------------------------------------------------


def _llm_output(name: str = "aria") -> str:
    return (
        "DESCRIPTION: A project.\n"
        "TAGLINE: in flight.\n"
        "\n"
        f"# {name}\n"
        "\n"
        "## Current state\n"
        "Going well.\n"
        "\n"
        "## Long-term decisions\n"
        "- decision A\n"
        "- decision B\n"
    )


def test_parse_extracts_description_tagline_and_strips_them():
    doc = ProjectDoc.parse(_llm_output(), name="aria")
    assert doc.description == "A project."
    assert doc.tagline == "in flight."
    assert doc.has_h1 is True
    # body is the post-H1 portion
    assert "DESCRIPTION:" not in doc.body
    assert "# aria" not in doc.body
    assert "## Current state" in doc.body


def test_render_body_inserts_headline_below_h1():
    doc = ProjectDoc.parse(_llm_output(), name="aria")
    fm = {
        "description": "A project.",
        "tagline": "in flight.",
        "decisions": 2,
        "open_threads": 1,
        "risks": 0,
    }
    out = doc.render_body(front_matter=fm)
    assert out.startswith("# aria\n\n")
    assert "> **aria** — A project." in out
    assert "> **Now:** in flight." in out
    assert "`2 decisions · 1 open`" in out
    # headline sits before Current state
    assert out.index("> **aria**") < out.index("## Current state")


def test_render_body_idempotent_via_parse_loop():
    """Render a doc, re-parse it, re-render — should be byte-identical."""
    doc1 = ProjectDoc.parse(_llm_output(), name="aria")
    fm = {"description": "A project.", "tagline": "in flight.", "decisions": 2}
    once = doc1.render_body(front_matter=fm)
    # Re-parse the rendered output (with synthetic DESC/TAG preamble for parse)
    re_input = f"DESCRIPTION: {fm['description']}\nTAGLINE: {fm['tagline']}\n\n{once}"
    doc2 = ProjectDoc.parse(re_input, name="aria")
    twice = doc2.render_body(front_matter=fm)
    assert once == twice
    # Quote line appears exactly once
    assert once.count("> **aria** — A project.") == 1


def test_render_body_replaces_prior_headline_with_new_data():
    doc = ProjectDoc.parse(_llm_output(), name="aria")
    fm_old = {"description": "old desc", "tagline": "old tag", "decisions": 1}
    after_old = doc.render_body(front_matter=fm_old)

    re_input = f"DESCRIPTION: {fm_old['description']}\nTAGLINE: {fm_old['tagline']}\n\n{after_old}"
    doc2 = ProjectDoc.parse(re_input, name="aria")
    fm_new = {
        "description": "new desc",
        "tagline": "new tag",
        "decisions": 5,
        "open_threads": 1,
    }
    after_new = doc2.render_body(front_matter=fm_new)
    assert "old desc" not in after_new
    assert "old tag" not in after_new
    assert "new desc" in after_new
    assert "new tag" in after_new
    assert after_new.count("> **aria**") == 1
    assert "`5 decisions · 1 open`" in after_new


def test_render_body_preserves_user_preamble_between_h1_and_section():
    """If the user hand-edits a note between the H1 and the first ##,
    the next render must not destroy it."""
    body_with_user_note = (
        "# aria\n"
        "\n"
        "> **aria** — old desc\n"
        "\n"
        "`1 decisions`\n"
        "\n"
        "User-added note: don't refactor X without checking Y.\n"
        "\n"
        "## Current state\n"
        "stuff\n"
    )
    re_input = f"DESCRIPTION: old desc\nTAGLINE: t\n\n{body_with_user_note}"
    doc = ProjectDoc.parse(re_input, name="aria")
    fm = {"description": "new desc", "tagline": "new tag", "decisions": 2}
    out = doc.render_body(front_matter=fm)
    # The headline is replaced
    assert "old desc" not in out
    assert "new desc" in out
    # The user note survives
    assert "User-added note: don't refactor X without checking Y." in out


def test_render_body_no_h1_returns_body_unchanged():
    raw = "no heading here\nsome text"
    doc = ProjectDoc.parse(raw, name="aria")
    assert doc.has_h1 is False
    out = doc.render_body(front_matter={"description": "x"})
    assert out == raw


def test_parse_handles_code_fence_wrapper():
    fenced = "```markdown\n" + _llm_output() + "```\n"
    doc = ProjectDoc.parse(fenced, name="aria")
    assert doc.description == "A project."
    assert "## Current state" in doc.body


# ---------------------------------------------------------------------------
# build_headline_block — the derived view
# ---------------------------------------------------------------------------


def test_headline_block_omits_missing_stats_cleanly():
    fm = {"description": "desc only", "tagline": "tag only"}
    lines = build_headline_block("aria", fm)
    out = "\n".join(lines)
    assert "> **aria** — desc only" in out
    assert "> **Now:** tag only" in out
    assert "`" not in out
    assert "momentum" not in out
    assert "last session" not in out


def test_headline_block_includes_momentum_and_days_when_present():
    fm = {
        "description": "desc",
        "tagline": "tag",
        "decisions": 4,
        "open_threads": 2,
        "risks": 1,
        "days_since_last_session": 3,
        "momentum": "hot",
    }
    out = "\n".join(build_headline_block("aria", fm))
    assert (
        "`4 decisions · 2 open · 1 risks · last session 3d ago · momentum: hot`"
        in out
    )


def test_headline_block_omits_missing_description_or_tagline():
    fm = {"decisions": 2}
    lines = build_headline_block("aria", fm)
    # Quote is project name only (no em dash + desc)
    assert lines[0] == "> **aria**"
    # No "Now:" line because tagline is empty
    assert not any(l.startswith("> **Now:**") for l in lines)
    out = "\n".join(lines)
    assert "`2 decisions`" in out


# ---------------------------------------------------------------------------
# diff_front_matter — pure-function changelog diff
# ---------------------------------------------------------------------------


def test_diff_first_rollup_all_positive():
    new_fm = {
        "decisions": 2,
        "open_threads": 3,
        "preferences": 0,
        "risks": 1,
        "sessions_last_7d": 1,
        "last_updated": "2026-04-25T12:00:00Z",
    }
    line = diff_front_matter({}, new_fm)
    assert line.startswith("- 2026-04-25T12:00:00Z — ")
    assert "+2 decisions" in line
    assert "+3 open" in line
    assert "+1 risk" in line
    assert "preference" not in line
    assert "sessions_last_7d 0→1" in line


def test_diff_field_order():
    prior = {
        "decisions": 0,
        "open_threads": 0,
        "preferences": 0,
        "risks": 0,
        "sessions_last_7d": 0,
    }
    new = {
        "decisions": 1,
        "open_threads": 1,
        "preferences": 1,
        "risks": 1,
        "sessions_last_7d": 1,
        "last_updated": "2026-04-25T12:00:00Z",
    }
    line = diff_front_matter(prior, new)
    i_dec = line.index("decision")
    i_open = line.index("open")
    i_pref = line.index("preference")
    i_risk = line.index("risk")
    assert i_dec < i_open < i_pref < i_risk


def test_diff_only_changed_fields():
    prior = {
        "decisions": 5,
        "open_threads": 4,
        "preferences": 2,
        "risks": 1,
        "sessions_last_7d": 3,
    }
    new = {
        "decisions": 7,
        "open_threads": 1,
        "preferences": 2,
        "risks": 2,
        "sessions_last_7d": 3,
        "last_updated": "2026-04-25T12:00:00Z",
    }
    line = diff_front_matter(prior, new)
    assert "+2 decisions" in line
    assert "-3 open" in line
    assert "+1 risk" in line
    assert "preference" not in line
    assert "sessions_last_7d" not in line


def test_diff_singular_plural():
    prior = {"decisions": 0, "open_threads": 0, "preferences": 0, "risks": 0}
    new = {
        "decisions": 1,
        "open_threads": 2,
        "preferences": 0,
        "risks": 0,
        "sessions_last_7d": 0,
        "last_updated": "2026-04-25T12:00:00Z",
    }
    line = diff_front_matter(prior, new)
    assert "+1 decision" in line and "+1 decisions" not in line
    assert "+2 open" in line


def test_diff_no_change_returns_empty():
    fm = {
        "decisions": 3,
        "open_threads": 2,
        "preferences": 1,
        "risks": 0,
        "sessions_last_7d": 4,
        "last_updated": "2026-04-25T12:00:00Z",
    }
    assert diff_front_matter(fm, fm) == ""


def test_diff_momentum_only_when_present_and_changed():
    prior = {
        "decisions": 1, "open_threads": 1, "preferences": 0, "risks": 0,
        "sessions_last_7d": 1, "momentum": "cold",
    }
    new = {
        "decisions": 1, "open_threads": 1, "preferences": 0, "risks": 0,
        "sessions_last_7d": 1, "momentum": "rising",
        "last_updated": "2026-04-25T12:00:00Z",
    }
    line = diff_front_matter(prior, new)
    assert "momentum cold→rising" in line

    line2 = diff_front_matter(
        {**prior, "momentum": None}, {**new, "momentum": None}
    )
    assert line2 == ""

    same = {**prior, "momentum": "rising"}
    new_same = {**new, "momentum": "rising"}
    assert diff_front_matter(same, new_same) == ""
