"""The voice lint: each rule fires on its habit and stays quiet on on-voice replies."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from evals.voice.lint import Flag, lint_reply
from prompts.agent import EXAMPLES

VOICE_DOC = Path(__file__).parents[2] / "docs" / "voice.md"


def _quoted(text: str) -> list[str]:
    """Each blockquote in ``text``, as one string."""
    replies: list[str] = []
    for block in re.finditer(r"(?:^>.*\n?)+", text, re.M):
        lines = [line.removeprefix(">").strip() for line in block.group(0).splitlines()]
        replies.append("\n".join(lines).strip())
    return replies


def _calibration() -> list[str]:
    """The operator's §13 samples, before the prompt's own examples begin."""
    doc = VOICE_DOC.read_text()
    section = doc[doc.index("## 13. Calibration") : doc.index("### In Talos's prompt")]
    return _quoted(section)


def rules(text: str) -> set[str]:
    return {flag.rule for flag in lint_reply(text)}


def test_the_calibration_samples_are_found() -> None:
    assert len(_calibration()) == 8


@pytest.mark.parametrize("reply", _calibration())
def test_calibration_samples_raise_nothing(reply: str) -> None:
    assert lint_reply(reply) == []


def test_the_prompt_examples_raise_nothing_and_match_the_doc() -> None:
    replies = _quoted(EXAMPLES)
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
        ("Fixed. Shall I push it?", "trailing-offer"),
        ("lol yeah, gonna be fine dude.", "informal"),
        ("Yep, it's 429.", "informal"),
        ("Skynet will nuke you before it reboots.", "dark"),
        ("Green. The robot uprising can wait.", "dark"),
        ("Green. I don't need sleep, but you do.", "stock-machine"),
        ("If I had feelings, this suite would hurt them.", "stock-machine"),
        ("I don't sleep, so no apology needed.", "stock-machine"),
        ("I never get tired.", "stock-machine"),
        ("As an AI, I do not get tired.", "stock-machine"),
        ("I don't keep hours, which makes one of us.", "stock-machine"),
        ("Judgement Day can wait.", "dark"),
        ("Done. Shall I also add benchmarks or tests?", "trailing-offer"),
        ("Perhaps you could try raising the memory limit.", "hedge"),
        ("It has become self-aware. Hide the launch codes.", "pop-culture"),
        ("Skynet would be proud.", "pop-culture"),
        ("I'm sorry, Dave. It won't reboot.", "pop-culture"),
    ],
)
def test_each_rule_fires_on_its_habit(reply: str, rule: str) -> None:
    assert rule in rules(reply)


@pytest.mark.parametrize(
    "reply",
    [
        "Exit code 137 means the kernel killed it; the OOM killer is the usual culprit.",
        "The worker is a zombie process; reap it with `wait`.",
    ],
)
def test_on_voice_lines_that_look_like_habits_pass(reply: str) -> None:
    assert lint_reply(reply) == []


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
