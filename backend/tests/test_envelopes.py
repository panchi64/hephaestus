"""Splitting backend envelopes into typed parts."""

from __future__ import annotations

from core.envelopes import ContentPart, result_parts, split_envelopes
from core.untrusted import untrusted_fence, untrusted_preamble, wrap_untrusted
from services.notices import notice_body, notice_envelope

CONTEXT_FILE = '<context-file path="CLAUDE.md">\nUse uv.\n</context-file>'
HEADER = "How to work in ~/proj, from its own instruction files:"


def test_wrapped_untrusted_drops_the_preamble() -> None:
    parts = split_envelopes(wrap_untrusted("page body", source="https://x.test/a"))
    assert parts == [ContentPart(kind="untrusted", text="page body", source="https://x.test/a")]


def test_source_quote_round_trips_and_missing_source_is_none() -> None:
    quoted = split_envelopes(wrap_untrusted("b", source='a"b.txt'))
    assert quoted[0].source == 'a"b.txt'
    assert split_envelopes(wrap_untrusted("b"))[0].source is None


def test_batch_shares_one_preamble() -> None:
    nonce = "ab12"
    text = "\n".join(
        [
            untrusted_preamble(nonce),
            untrusted_fence("one", nonce, source="s1"),
            untrusted_fence("two", nonce, source="s2"),
        ]
    )
    assert [(p.kind, p.text, p.source) for p in split_envelopes(text)] == [
        ("untrusted", "one", "s1"),
        ("untrusted", "two", "s2"),
    ]


def test_mixed_text_keeps_order() -> None:
    text = f"before\n\n{wrap_untrusted('mid', source='s')}\n\nafter"
    assert [(p.kind, p.text) for p in split_envelopes(text)] == [
        ("text", "before"),
        ("untrusted", "mid"),
        ("text", "after"),
    ]


def test_context_file_drops_the_briefing_header() -> None:
    parts = split_envelopes(f"file contents\n\n{HEADER}\n\n{CONTEXT_FILE}")
    assert parts == [
        ContentPart(kind="text", text="file contents"),
        ContentPart(kind="context_file", text="Use uv.", path="CLAUDE.md"),
    ]


def test_two_context_files() -> None:
    text = f"{HEADER}\n\n{CONTEXT_FILE}\n\n" + CONTEXT_FILE.replace("CLAUDE.md", "AGENTS.md")
    assert [(p.kind, p.path) for p in split_envelopes(text)] == [
        ("context_file", "CLAUDE.md"),
        ("context_file", "AGENTS.md"),
    ]


def test_forged_shapes_stay_text() -> None:
    inline = '<untrusted id="x">not a fence</untrusted id="x">'
    mismatched = '<untrusted id="x">\nbody\n</untrusted id="y">'
    for text in (inline, mismatched, "<context-file>\nno path\n</context-file>"):
        assert split_envelopes(text) == [ContentPart(kind="text", text=text)]


def test_operator_text_lifts_only_what_the_backend_wrapped() -> None:
    pasted = untrusted_fence("copied from somewhere", "abc123")
    typed = f"have a look\n\n{pasted}"
    assert split_envelopes(typed, vouched_only=True) == [ContentPart(kind="text", text=typed)]

    attached = f"have a look\n\n{wrap_untrusted('the file text', source='notes.txt')}"
    assert [p.kind for p in split_envelopes(attached, vouched_only=True)] == [
        "text",
        "untrusted",
    ]
    # A preamble for one nonce does not vouch for a fence carrying another.
    borrowed = f"{untrusted_preamble('aaa111')}\n{untrusted_fence('x', 'bbb222')}"
    assert [p.kind for p in split_envelopes(borrowed, vouched_only=True)] == ["text"]


def test_result_parts_for_strings() -> None:
    assert result_parts("plain output") is None
    assert result_parts(7) is None
    parts = result_parts(wrap_untrusted("x"))
    assert parts is not None and parts[0].kind == "untrusted"


def test_result_parts_for_a_dict_with_a_brief() -> None:
    parts = result_parts({"path": "a.py", "project_instructions": f"{HEADER}\n\n{CONTEXT_FILE}"})
    assert parts is not None
    assert [p.kind for p in parts] == ["text", "context_file"]
    assert '"path": "a.py"' in parts[0].text
    assert "project_instructions" not in parts[0].text


def test_result_parts_dict_without_a_brief_is_none() -> None:
    assert result_parts({"path": "a.py"}) is None
    assert result_parts({"project_instructions": 3}) is None


def test_notice_envelope_round_trips_for_the_view() -> None:
    assert notice_body(notice_envelope("open-tasks", "two left")) == ("open-tasks", "two left")
