from __future__ import annotations

from sagent.frontmatter import (
    cap_description,
    split_front_matter,
    strip_front_matter,
    to_front_matter,
)


def test_emits_basic_block():
    out = to_front_matter({"type": "project", "n": 5, "active": True, "x": None})
    assert out.startswith("---\n")
    assert out.endswith("\n---\n")
    assert 'type: "project"' in out
    assert "n: 5" in out
    assert "active: true" in out
    assert "x: null" in out


def test_round_trip_preserves_strings():
    src = {
        "type": "session",
        "gist": 'A "quoted" thing with \\ backslash',
        "count": 42,
    }
    body = to_front_matter(src) + "\n# rest\n"
    parsed, rest = split_front_matter(body)
    assert parsed["type"] == "session"
    assert parsed["gist"] == 'A "quoted" thing with \\ backslash'
    assert parsed["count"] == 42
    assert rest.startswith("# rest")


def test_split_returns_empty_when_no_front_matter():
    parsed, rest = split_front_matter("# just markdown\n\nbody")
    assert parsed == {}
    assert rest == "# just markdown\n\nbody"


def test_strip_front_matter_returns_body():
    text = '---\nfoo: "bar"\n---\n# heading\nbody\n'
    assert strip_front_matter(text) == "# heading\nbody\n"


def test_strip_front_matter_passthrough_when_absent():
    assert strip_front_matter("# heading\n") == "# heading\n"


def test_cap_description_short_unchanged():
    assert cap_description("short text") == "short text"


def test_cap_description_truncates_at_word():
    src = "this is a sentence with several words " * 20
    out = cap_description(src, max_chars=80)
    assert len(out) <= 80
    assert out.endswith("…")


def test_cap_description_strips_trailing_punct():
    src = "first sentence, second sentence; third sentence " * 20
    out = cap_description(src, max_chars=50)
    assert out[-2] not in ",.;:"


def test_list_value_emitted_inline():
    out = to_front_matter({"tags": ["a", "b", "c"]})
    assert 'tags: ["a", "b", "c"]' in out


def test_value_with_newline_collapses():
    out = to_front_matter({"x": "line one\nline two"})
    assert 'x: "line one line two"' in out


def test_int_and_bool_round_trip():
    text = to_front_matter({"n": 17, "ok": True, "empty": False})
    parsed, _ = split_front_matter(text + "body")
    assert parsed == {"n": 17, "ok": True, "empty": False}
