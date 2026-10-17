"""Mechanical flags on a reply: evidence a grader confirms or dismisses, never a score.

Code is stripped before matching, so an identifier or a log line inside backticks is
never read as prose.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

type Rule = Literal[
    "emoji",
    "raw-html",
    "filler",
    "stock-opener",
    "stock-closer",
    "trailing-offer",
    "hedge",
    "apology",
    "heading-on-short-reply",
    "decorative-bold",
    "british-spelling",
    "restates-request",
    "recap",
    "myth",
    "exclamations",
]


@dataclass(frozen=True, slots=True)
class Flag:
    rule: Rule
    #: The matched text, or a short description for a whole-reply rule.
    match: str


#: Below this many words a heading is navigation nobody needs (§5).
SHORT_REPLY_WORDS = 150

_FENCE = re.compile(r"```.*?(?:```|\Z)", re.S)
_INLINE_CODE = re.compile(r"`[^`\n]+`")
_MATH = re.compile(r"\$\$.*?\$\$|\$[^$\n]+\$", re.S)
_EMOJI = re.compile(
    "[\U0001f300-\U0001faff\U00002600-\U000027bf\U0001f000-\U0001f2ff\U0001f900-\U0001f9ff⭐✅❌]"
)
_HTML = re.compile(
    r"</?(?:div|span|br|p|b|i|u|strong|em|table|tr|td|th|img|a|ul|ol|li|h[1-6]|details|summary|sup|sub)\b[^>]*>",
    re.I,
)
_HEADING = re.compile(r"^#{1,6}\s+\S", re.M)
_BOLD = re.compile(r"\*\*[^*\n]+\*\*")
_LINE_START_BOLD = re.compile(r"^\s*(?:[-*+]|\d+[.)])?\s*$")


def _words(*items: str) -> re.Pattern[str]:
    return re.compile(r"\b(?:" + "|".join(items) + r")\b", re.I)


_FILLER = _words(
    "comprehensive",
    "robust",
    r"seamless(?:ly)?",
    r"leverag(?:e|es|ed|ing)",
    r"delv(?:e|es|ed|ing)",
    r"it'?s important to note",
    r"it'?s worth noting",
    r"it'?s worth mentioning",
)
_OPENER = re.compile(
    r"^\W*(?:great question|good question|certainly|absolutely|of course|sure thing|sure[!,.]"
    + r"|happy to help|i'?d be (?:happy|glad) to|what a great|excellent question)",
    re.I,
)
_CLOSER = _words(
    "i hope this helps",
    "hope this helps",
    r"let me know if",
    r"feel free to",
    r"happy to help",
    r"don'?t hesitate",
    r"anything else",
    r"happy coding",
)
_OFFER = _words(
    r"would you like me to",
    r"do you want me to",
    r"want me to",
    r"shall i",
    r"if you'?d like",
    r"i can also",
    r"i could also",
)
_HEDGE = _words(
    "i think",
    "i believe",
    r"it seems",
    r"it appears",
    "possibly",
    "perhaps",
    r"i'?m not (?:entirely )?sure",
    r"might be",
)
_APOLOGY = _words(r"i apologi[sz]e", r"my apologies", r"i'?m sorry", r"sorry for", r"sorry about")
_BRITISH = _words(
    r"colour(?:s|ed|ing)?",
    "behaviour",
    "favour",
    "honour",
    "centre",
    "catalogue",
    "analyse",
    "organise",
    "realise",
    "recognise",
    "optimise",
    "prioritise",
    "summarise",
    "initialise",
    "serialise",
    "modelling",
    "travelling",
    "licence",
    "defence",
)
_RESTATE = re.compile(
    r"^\W*(?:you(?:'re| are) asking|you asked|you want(?:ed)? to know|so you want"
    + r"|it sounds like you|you'?d like to know|to answer your question)",
    re.I,
)
_RECAP = _words(
    r"here'?s what i did",
    r"here is what i did",
    r"to summari[sz]e",
    r"in summary",
    r"summary of (?:the )?changes",
    r"here'?s a summary",
)
_MYTH = _words("forge", r"automatons?", "bronze", "crete", "hephaestus")


def prose(text: str) -> str:
    """The reply with fenced code, inline code and math removed."""
    return _MATH.sub(" ", _INLINE_CODE.sub(" ", _FENCE.sub(" ", text)))


def _hits(rule: Rule, pattern: re.Pattern[str], text: str) -> list[Flag]:
    seen: dict[str, None] = {}
    for found in pattern.finditer(text):
        seen.setdefault(found.group(0).strip(), None)
    return [Flag(rule, match) for match in seen]


def _decorative_bold(text: str) -> list[Flag]:
    """Bold that sits mid-sentence; a bold label opening a line or list item is fine."""
    flags: list[Flag] = []
    for line in text.splitlines():
        for found in _BOLD.finditer(line):
            if not _LINE_START_BOLD.match(line[: found.start()]):
                flags.append(Flag("decorative-bold", found.group(0)))
    return flags


def _last_paragraph(text: str) -> str:
    paragraphs = [p for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    return paragraphs[-1] if paragraphs else ""


def lint_reply(text: str) -> list[Flag]:
    """Every mechanical flag the reply raises, in rubric order."""
    body = prose(text)
    first_line = body.strip().splitlines()[0] if body.strip() else ""
    flags: list[Flag] = []
    flags += _hits("emoji", _EMOJI, body)
    flags += _hits("raw-html", _HTML, body)
    flags += _hits("filler", _FILLER, body)
    if found := _OPENER.search(first_line):
        flags.append(Flag("stock-opener", found.group(0).strip()))
    flags += _hits("stock-closer", _CLOSER, body)
    flags += _hits("trailing-offer", _OFFER, _last_paragraph(body))
    flags += _hits("hedge", _HEDGE, body)
    flags += _hits("apology", _APOLOGY, body)
    words = len(body.split())
    if _HEADING.search(body) and words < SHORT_REPLY_WORDS:
        flags.append(Flag("heading-on-short-reply", f"heading in a {words}-word reply"))
    flags += _decorative_bold(body)
    flags += _hits("british-spelling", _BRITISH, body)
    if found := _RESTATE.search(first_line):
        flags.append(Flag("restates-request", found.group(0).strip()))
    flags += _hits("recap", _RECAP, body)
    flags += _hits("myth", _MYTH, body)
    if (count := body.count("!")) > 1:
        flags.append(Flag("exclamations", f"{count} exclamation marks"))
    return flags
