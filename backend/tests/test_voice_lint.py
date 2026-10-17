"""The voice lint: each rule fires on its habit and stays quiet on on-voice replies."""

from __future__ import annotations

import pytest

from evals.voice.lint import Flag, lint_reply

# docs/voice.md §13, verbatim: on-voice by definition, so they must raise nothing.
CALIBRATION = [
    "Rollback journal copies each page *before* changing it, then writes the database in "
    "place; a crash rolls back from the copy. Writers block readers.\n\n"
    "WAL does the inverse: changes append to a separate `-wal` file and the database is "
    "updated later at checkpoint. Readers keep reading the old pages while one writer "
    "appends, so concurrency is far better. The cost is an extra file and occasional "
    "checkpoint stalls.\n\nFor your setup (one process, many async readers), WAL is the "
    "right call.",
    "Found it. `conftest.py` shares one engine across tests, so each test inherits the last "
    "one's open transaction. I've switched it to a per-test engine, and the suite is green, "
    "all 214. Ran it twice to be sure nothing else is hiding in there.",
    "Before I do: the live database at `data/app.db` is at revision `a41c`. Regenerating "
    "gives the history new IDs, so Alembic will no longer recognize that database and the "
    "next upgrade will try to recreate tables that already exist.\n\nIf what you want is a "
    "clean history, I can squash everything up to `a41c` into one baseline migration and "
    "stamp the database. Same result, and nothing breaks. Squash, or delete anyway?",
    "It's past two. The tests will still be passing in the morning; I checked.",
]


def rules(text: str) -> set[str]:
    return {flag.rule for flag in lint_reply(text)}


@pytest.mark.parametrize("reply", CALIBRATION)
def test_calibration_samples_raise_nothing(reply: str) -> None:
    assert lint_reply(reply) == []


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
