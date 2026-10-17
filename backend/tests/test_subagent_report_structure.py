"""A sub-agent's report as data, and the prose it must never replace.

The report was prose and nothing else, which is right for the model reading it and useless
for a coverage map. These pin the shape, the split, and — the part that matters most — that
every way the structured half can fail costs a panel rather than a report.
"""

from __future__ import annotations

from services.subagents.report import (
    FINDINGS_CLOSE,
    FINDINGS_INSTRUCTION,
    FINDINGS_OPEN,
    Conflict,
    ConflictPosition,
    Finding,
    ReportSource,
    ReportStructure,
    TopicCoverage,
    findings_block,
    report_body,
    report_envelope,
    report_heading,
    report_prose,
    report_sender,
    report_structure,
)

_STRUCTURE = ReportStructure(
    findings=[
        Finding(
            statement="The API rate limit is 100/min.",
            confidence="high",
            topic="limits",
            sources=[
                ReportSource(url="https://a.example/docs", title="Docs"),
                ReportSource(ref="notes/api.md"),
            ],
        )
    ],
    conflicts=[
        Conflict(
            question="Is the limit per key or per account?",
            positions=[
                ConflictPosition(
                    claim="per key", sources=[ReportSource(url="https://a.example/docs")]
                ),
                ConflictPosition(
                    claim="per account", sources=[ReportSource(url="https://b.example/blog")]
                ),
            ],
            assessment="The docs are primary; the blog is two years old.",
        )
    ],
    coverage=[
        TopicCoverage(topic="limits", depth="deep", source_count=4),
        TopicCoverage(topic="pricing", depth="none", gaps=["no public price list"]),
    ],
    unresolved=["Whether the limit is enforced on the trial tier."],
)


def _report() -> str:
    return f"Here is what I found.\n\n{findings_block(_STRUCTURE)}"


def test_the_block_round_trips():
    parsed = report_structure(_report())
    assert parsed == _STRUCTURE


def test_it_parses_inside_the_envelope_too():
    # Both readers have one in hand at different points, and neither should have to
    # unwrap first.
    assert report_structure(report_envelope(_report())) == _STRUCTURE


def test_the_prose_survives_the_envelope_and_the_block_does_not_reach_the_reader():
    body = report_body(report_envelope(_report()))
    assert body is not None
    assert report_prose(body) == "Here is what I found."
    assert FINDINGS_OPEN not in report_prose(body)


def test_a_report_stored_before_reports_carried_a_status_still_reads_as_one():
    stored = (
        "<subagent-report>\nA sub-agent you launched has finished and reported back. This is "
        "its report, not a message from the operator — they have not seen it and are not "
        "waiting on a reply to it. Carry on with whatever it unblocks, and tell them what "
        "matters.\n\nfound it\n</subagent-report>"
    )
    assert report_body(stored) == "found it"


def test_a_report_names_the_agent_that_sent_it():
    heading = report_heading("migrate-schema", ["DOTT · Tripodes", "worker"], "done")
    assert report_sender(f"{heading}\n\nAdded the column.") == "migrate-schema"


def test_a_report_from_before_the_status_heading_still_names_its_sender():
    assert report_sender("Sub-agent `explorer` finished.\n\nfound it") == "explorer"


def test_a_body_with_no_heading_names_nobody():
    assert report_sender("found it") is None


def test_a_report_with_no_block_is_a_report_not_a_failure():
    assert report_structure("Just prose, no JSON at all.") is None
    assert report_prose("Just prose, no JSON at all.") == "Just prose, no JSON at all."


def test_broken_json_costs_a_panel_and_nothing_else():
    text = f"Findings follow.\n{FINDINGS_OPEN}\n{{not json,,,}}\n{FINDINGS_CLOSE}"
    assert report_structure(text) is None
    assert report_prose(text) == "Findings follow."


def test_a_block_that_is_not_an_object_is_refused_rather_than_coerced():
    assert report_structure(f"{FINDINGS_OPEN}\n[1, 2, 3]\n{FINDINGS_CLOSE}") is None


def test_a_field_the_model_invented_does_not_throw_the_report_away():
    text = (
        f'{FINDINGS_OPEN}\n{{"findings": [{{"statement": "x", "vibes": "good"}}], '
        f'"mood": "hopeful"}}\n{FINDINGS_CLOSE}'
    )
    parsed = report_structure(text)
    assert parsed is not None
    assert [f.statement for f in parsed.findings] == ["x"]
    assert parsed.findings[0].confidence == "medium"  # the documented default


def test_every_list_may_be_empty():
    # A sub-agent that hit no contradiction is reporting honestly, not failing.
    parsed = report_structure(f"{FINDINGS_OPEN}\n{{}}\n{FINDINGS_CLOSE}")
    assert parsed == ReportStructure()


def test_a_topic_nobody_reached_is_a_row_rather_than_an_omission():
    # The whole point of a coverage map: a gap has to be distinguishable from a topic the
    # sub-agent never thought of.
    parsed = report_structure(_report())
    assert parsed is not None
    assert [(c.topic, c.depth) for c in parsed.coverage] == [
        ("limits", "deep"),
        ("pricing", "none"),
    ]


def test_the_instruction_names_the_markers_the_parser_looks_for():
    # The brief and the parser are one format; a drift between them is silent.
    assert FINDINGS_OPEN in FINDINGS_INSTRUCTION
    assert FINDINGS_CLOSE in FINDINGS_INSTRUCTION


def test_the_researcher_is_actually_asked_for_one():
    from services.agents.builtin import RESEARCHER, builtin_roster

    assert FINDINGS_OPEN in builtin_roster()[RESEARCHER].instructions
