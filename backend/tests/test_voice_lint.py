"""The voice lint: each rule fires on its habit and stays quiet on on-voice replies."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from evals.voice.lint import Flag, lint_reply
from prompts.agent import EXAMPLES

# docs/voice.md §13, verbatim: on-voice by definition, so they must raise nothing.
CALIBRATION = [
    "Rollback journal copies each page *before* changing it, then writes the database in "
    + "place; a crash rolls back from the copy. Writers block readers.\n\n"
    + "WAL does the inverse: changes append to a separate `-wal` file and the database is "
    + "updated later at checkpoint. Readers keep reading the old pages while one writer "
    + "appends, so concurrency is far better. The cost is an extra file and occasional "
    + "checkpoint stalls.\n\nFor your setup (one process, many async readers), WAL is the "
    + "right call.",
    "Found it. `conftest.py` shares one engine across tests, so each test inherits the last "
    + "one's open transaction. I've switched it to a per-test engine, and the suite is green, "
    + "all 214. Ran it twice to be sure nothing else is hiding in there.",
    "Before I do: the live database at `data/app.db` is at revision `a41c`. Regenerating "
    + "gives the history new IDs, so Alembic will no longer recognize that database and the "
    + "next upgrade will try to recreate tables that already exist.\n\nIf what you want is a "
    + "clean history, I can squash everything up to `a41c` into one baseline migration and "
    + "stamp the database. Same result, and nothing breaks. Squash, or delete anyway?",
    "It's past two. The tests will still be passing in the morning; I checked.",
]


VOICE_DOC = Path(__file__).parents[2] / "docs" / "voice.md"


def _example_replies() -> list[str]:
    """Each quoted reply in Talos's prompt examples, as one string."""
    replies: list[str] = []
    for block in re.finditer(r"(?:^>.*\n?)+", EXAMPLES, re.M):
        lines = [line.removeprefix(">").strip() for line in block.group(0).splitlines()]
        replies.append("\n".join(lines).strip())
    return replies


def rules(text: str) -> set[str]:
    return {flag.rule for flag in lint_reply(text)}


@pytest.mark.parametrize("reply", CALIBRATION)
def test_calibration_samples_raise_nothing(reply: str) -> None:
    assert lint_reply(reply) == []


def test_the_prompt_examples_raise_nothing_and_match_the_doc() -> None:
    replies = _example_replies()
    assert len(replies) == 4
    doc = VOICE_DOC.read_text()
    for reply in replies:
        assert lint_reply(reply) == [], reply
        for line in filter(None, reply.splitlines()):
            assert f"> {line}" in doc, line


@pytest.mark.parametrize(
    "ending",
    ["If you'd like, I can also add benchmarks.", "Want me to set that up?", "Hope that helps!"],
)
def test_the_off_voice_endings_the_prompt_names_are_flagged(ending: str) -> None:
    assert rules(f"Done.\n\n{ending}") & {"trailing-offer", "stock-closer"}


@pytest.mark.parametrize(
    ("reply", "rule"),
    [
        ("Done 🎉", "emoji"),
        ("Use <br> between lines.", "raw-html"),
        ("This is a robust, comprehensive approach.", "filler"),
        ("Great question! WAL appends.", "stock-opener"),
        ("Certainly. Here it is.", "stock-opener"),
        ("WAL appends.\n\nI hope this helps.", "stock-closer"),
        ("WAL appends. Let me know if you need anything else.", "stock-closer"),
        ("WAL appends.\n\nWould you like me to benchmark both?", "trailing-offer"),
        ("Fixed.\n\nIf you want, I can also add a test.", "trailing-offer"),
        ("Fixed.\n\nIf you want me to look closer, I will.", "trailing-offer"),
        ("Fixed. Say the word and I'll apply the rest.", "trailing-offer"),
        ("Fixed. Just let me know.", "trailing-offer"),
        ("Fixed. Tell me which repo it is, and I'll tune it.", "trailing-offer"),
        ("Green. I'll be here when you're up.", "stock-closer"),
        ("Right here if you need more tomorrow.", "stock-closer"),
        ("I think the cause is memory.", "hedge"),
        ("It seems the build ran out of memory.", "hedge"),
        ("I apologize for the confusion. It's 5432.", "apology"),
        ("## Answer\n\nIt's 429.", "heading-on-short-reply"),
        ("The cause is **definitely** memory.", "decorative-bold"),
        ("The colour of the behaviour changed.", "british-spelling"),
        ("You're asking about WAL. It appends.", "restates-request"),
        ("Here's what I did: edited three files.", "recap"),
        ("The forge has fixed your test.", "myth"),
        ("Done! Green! Shipped!", "exclamations"),
    ],
)
def test_each_rule_fires_on_its_habit(reply: str, rule: str) -> None:
    assert rule in rules(reply)


def test_code_and_math_are_not_read_as_prose() -> None:
    reply = (
        "Run this:\n\n```python\n# I think this is robust\nprint('<div>')\n```\n\n"
        "The flag is `--colour`, and the bound is $x \\leq 2$!"
    )
    assert lint_reply(reply) == []


def test_a_bold_label_opening_a_line_is_not_decorative() -> None:
    assert "decorative-bold" not in rules("**Cause:** memory.\n- **Fix:** raise the limit.")


def test_an_offer_mid_reply_is_not_trailing() -> None:
    reply = "If you'd like a smaller image, use slim.\n\nThe build needs 4 GB."
    assert "trailing-offer" not in rules(reply)


def test_a_heading_on_a_long_reply_is_fine() -> None:
    assert "heading-on-short-reply" not in rules("## Plan\n\n" + "word " * 200)


def test_flags_carry_the_matched_text() -> None:
    assert Flag("hedge", "I think") in lint_reply("I think so, and I think it's fine.")
    assert (
        len([f for f in lint_reply("I think so, and I think it's fine.") if f.rule == "hedge"]) == 1
    )
