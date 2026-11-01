"""How a message crossing the link between a thread and its sub-agent is framed.

Two messages cross it: a **report** coming back when a sub-agent finishes, and a
**direction** going down when the launching agent redirects one still working.

Pydantic AI's only shape for a message from outside the model is ``UserPromptPart``, which
bare would claim the operator typed it (the model would answer as though they had, and
every replay would show it as theirs). A sub-agent has no operator at all, so unframed text
in its thread is from nobody. Both are wrapped here.

**One envelope, both roads.** A report arrives queued into a running turn or as the prompt
of a woken turn; the wording is identical so nothing downstream can tell the route.

**Two readers, one constant.** The model reads the envelope as plain framing; the
transcript recognises it to render a report. That reader matches the whole header (opener
and preamble), not the tag alone, because an operator can type angle brackets.

**It is a label, not a credential**: a text marker is forgeable by anyone who can type it,
and a forgery only buys a differently styled bubble in the forger's own thread. The
structural answer to "who sent this" is ``QueuedMessage.source``.

It lives in ``services/`` because both readers are below the agent layer, and two copies of
a marker work only while they agree.

**A report also has a structured half**, appended inside the same envelope (second section
below): findings, conflicts and per-topic coverage as data, so a fan-out of researchers
reads as a coverage map. The writer, the parser and the brief that asks for it must agree
on one format, hence the same home.
"""

from __future__ import annotations

import json
import logging
import re
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from core.serde import parse_json
from core.untrusted import neutralize_envelope_markup
from services.notices import ENVELOPE_TAGS

logger = logging.getLogger(__name__)

#: The opener the transcript matches on, and the closer that bounds the body.
REPORT_OPEN = "<subagent-report>"
REPORT_CLOSE = "</subagent-report>"

#: The pair a launching agent's redirection was framed in before agents messaged each
#: other. Still read, so older transcripts render.
DIRECTION_OPEN = "<subagent-direction>"
DIRECTION_CLOSE = "</subagent-direction>"

#: The pair a message between two agents of one delegation tree travels in.
MESSAGE_OPEN = "<agent-message>"
MESSAGE_CLOSE = "</agent-message>"

#: Said to the model, not the operator, and explicit that they have not seen the report;
#: stripped before the transcript shows it.
_REPORT_PREAMBLE = (
    "From the forge, not from the operator: a sub-agent you launched has reported back, and "
    "the operator has not seen this report and is not waiting on a reply to it. Its first "
    "line says whether the work is done, blocked or failed. Carry on with whatever it "
    "unblocks, and tell the operator what matters."
)

#: The preamble reports were written with before they carried a status. Still read, so
#: older transcripts render them as reports.
_LEGACY_REPORT_PREAMBLE = (
    "A sub-agent you launched has finished and reported back. This is its report, not a "
    "message from the operator — they have not seen it and are not waiting on a reply to "
    "it. Carry on with whatever it unblocks, and tell them what matters."
)

#: The mirror, read by the sub-agent. Explicit about who it is from, that it amends the
#: brief, and that nobody reads a reply: its one reply is the report it ends with.
_DIRECTION_PREAMBLE = (
    "The agent that launched you has sent you this. It is not from the operator, and it "
    "changes the task you were given — take it as amending your brief, and prefer it where "
    "the two disagree. Do not reply to it; carry on with the work and report as usual."
)


def _header(opener: str, preamble: str) -> str:
    """The whole header, which an envelope is recognised by: opener and preamble, never the
    tag alone, since anyone can type an opening tag."""
    return f"{opener}\n{preamble}\n\n"


_MESSAGE_PREAMBLE = (
    "Another agent in your delegation tree sent you this. It is not from the operator. "
    "Weigh it against your task, which it may amend if it comes from the agent that "
    "spawned you. To answer, send a message back with `agents_message`."
)

_REPORT_HEADER = _header(REPORT_OPEN, _REPORT_PREAMBLE)
_LEGACY_REPORT_HEADER = _header(REPORT_OPEN, _LEGACY_REPORT_PREAMBLE)
_DIRECTION_HEADER = _header(DIRECTION_OPEN, _DIRECTION_PREAMBLE)
_MESSAGE_HEADER = _header(MESSAGE_OPEN, _MESSAGE_PREAMBLE)

_SENDER_LINE = re.compile(r"From `(?P<sender>[^`\n]+)`:\n")


def _wrap(header: str, closer: str, text: str) -> str:
    return f"{header}{text.strip()}\n{closer}"


def _unwrap(header: str, closer: str, text: str) -> str | None:
    stripped = text.strip()
    if not stripped.startswith(header):
        return None
    return stripped[len(header) :].removesuffix(closer).strip()


def report_envelope(text: str) -> str:
    """A sub-agent's report, framed as one."""
    # The note follows the heading: report_sender reads the handle from the first line.
    safe = neutralize_envelope_markup(text, ENVELOPE_TAGS, note_after_heading=True)
    return _wrap(_REPORT_HEADER, REPORT_CLOSE, safe)


def report_body(text: str) -> str | None:
    """The report inside an envelope, or ``None`` when this is not one."""
    body = _unwrap(_REPORT_HEADER, REPORT_CLOSE, text)
    return body if body is not None else _unwrap(_LEGACY_REPORT_HEADER, REPORT_CLOSE, text)


def report_heading(handle: str, extras: list[str], status: str) -> str:
    """A report's first line: `` `handle` (extras): status. ``, which :func:`report_sender`
    reads back."""
    named = f"`{handle}`" + (f" ({', '.join(extras)})" if extras else "")
    return f"{named}: {status}."


#: Also matches the ``Sub-agent `handle` (…) finished.`` line older reports opened with.
_REPORT_SENDER = re.compile(r"(?:Sub-agent )?`(?P<handle>[^`\n]+)`")


def report_sender(body: str) -> str | None:
    """The handle a report's heading names, or ``None`` for a body without one."""
    match = _REPORT_SENDER.match(body)
    return None if match is None else match.group("handle")


def direction_body(text: str) -> str | None:
    """The direction inside an envelope, or ``None`` when this is not one."""
    return _unwrap(_DIRECTION_HEADER, DIRECTION_CLOSE, text)


def message_envelope(sender: str, text: str) -> str:
    """A message from the agent ``sender`` names, framed as one."""
    safe = neutralize_envelope_markup(text, ENVELOPE_TAGS)
    return _wrap(_MESSAGE_HEADER, MESSAGE_CLOSE, f"From `{sender}`:\n{safe}")


def message_body(text: str) -> tuple[str, str] | None:
    """The sender and text inside a message envelope, or ``None`` when this is not one."""
    body = _unwrap(_MESSAGE_HEADER, MESSAGE_CLOSE, text)
    if body is None:
        return None
    match = _SENDER_LINE.match(body)
    if match is None:
        return None
    return match.group("sender"), body[match.end() :].strip()


# The report's structured half: what was established, where sources disagree, and which
# corners nobody covered, as data a fan-out of researchers can be read from.
#
# Beside the prose, never instead of it. The block sits inside the same envelope, so a
# missing or malformed one costs a panel, not a report; parsing is lenient and logs rather
# than raising because a report is all that survives a sub-agent.

#: Bounds the JSON block. Its own markers rather than a ``` fence, since a research report
#: quotes code and markdown and a delimiter a source can contain would cut it wrongly.
FINDINGS_OPEN = "<subagent-findings>"
FINDINGS_CLOSE = "</subagent-findings>"

_FINDINGS_BLOCK = re.compile(
    re.escape(FINDINGS_OPEN) + r"\s*(?P<json>.*?)\s*" + re.escape(FINDINGS_CLOSE),
    re.DOTALL,
)

Confidence = Literal["high", "medium", "low"]

#: How well a topic was covered. ``none`` is a real answer: an omitted row would make a gap
#: indistinguishable from a topic the sub-agent never thought of.
Depth = Literal["none", "thin", "adequate", "deep"]


class _Lenient(BaseModel):
    """Unknown keys are dropped: an invented field by a model writing JSON by hand should
    not discard the report's structure."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")


class ReportSource(_Lenient):
    """Where a sub-agent read something: ``url`` for a page, ``ref`` for a passage of the
    operator's corpus, the same split as :class:`core.citations.Citation`."""

    url: str | None = None
    ref: str | None = None
    title: str | None = None


class Finding(_Lenient):
    """One thing the sub-agent established, and what it rests on.

    ``sources`` makes this a finding rather than an assertion: the count of independent
    sources is only a count if they are listed.
    """

    statement: str
    confidence: Confidence = "medium"
    sources: list[ReportSource] = []
    #: The investigation topic, matched by name against :class:`TopicCoverage.topic`. Free
    #: text, since topics are the launching agent's own words.
    topic: str | None = None


class ConflictPosition(_Lenient):
    """One side of a disagreement, and who is on it."""

    claim: str
    sources: list[ReportSource] = []


class Conflict(_Lenient):
    """A question the sources answer differently.

    Kept first-class because a summary that silently picks a side looks like sources that
    agreed. ``assessment`` says which position the sub-agent finds more credible and why.
    """

    question: str
    positions: list[ConflictPosition] = []
    assessment: str | None = None


class TopicCoverage(_Lenient):
    """How far the investigation actually got on one topic."""

    topic: str
    depth: Depth = "thin"
    source_count: int = 0
    #: What is missing, in the sub-agent's own words.
    gaps: list[str] = []


class ReportStructure(_Lenient):
    """The report as data. Every list may be empty."""

    findings: list[Finding] = []
    conflicts: list[Conflict] = []
    coverage: list[TopicCoverage] = []
    #: Questions the sub-agent could not settle, including any it was not equipped to.
    unresolved: list[str] = []


#: Appended to the briefs of sub-agents whose reports merit a panel. Explicit that the prose
#: comes first, or a sub-agent replacing its report with JSON would destroy what the
#: launching agent reads.
FINDINGS_INSTRUCTION = (
    "End your report with a machine-readable summary of it, after your prose and never "
    f"instead of it. Put it between {FINDINGS_OPEN} and {FINDINGS_CLOSE}, as one JSON "
    "object with these keys, each a list and each allowed to be empty:\n"
    '- "findings": {"statement", "confidence" (high|medium|low), "topic", '
    '"sources": [{"url", "ref", "title"}]} — one per thing you established, with every '
    "source you established it from, not just the best one.\n"
    '- "conflicts": {"question", "positions": [{"claim", "sources"}], "assessment"} — '
    "one per question your sources answered differently. Do not resolve a disagreement "
    "by leaving it out; say in `assessment` which you find more credible and why.\n"
    '- "coverage": {"topic", "depth" (none|thin|adequate|deep), "source_count", "gaps"} '
    "— one per topic you were asked about, including the ones you got nowhere on. A "
    '"none" row is more useful than a missing one.\n'
    '- "unresolved": plain strings, the questions you could not settle.\n'
    "Everything in it must also be in your prose. It is the same report read by "
    "something that cannot read prose, not a second, shorter report."
)


def findings_block(structure: ReportStructure) -> str:
    """``structure`` as the block a report ends with: the writer's half of the parser below."""
    payload = structure.model_dump(mode="json", exclude_none=True)
    return f"{FINDINGS_OPEN}\n{json.dumps(payload, indent=2)}\n{FINDINGS_CLOSE}"


def report_structure(text: str) -> ReportStructure | None:
    """The structured block inside a report, or ``None`` when there isn't a usable one.

    Takes the report with or without its envelope.

    **Never raises.** A missing, broken or non-object block costs a panel and nothing else;
    the prose is untouched. Failures are logged because a persistently unparseable block
    means the brief needs rewording.
    """
    match = _FINDINGS_BLOCK.search(text)
    if match is None:
        return None
    try:
        payload = parse_json(match.group("json"))
    except ValueError as exc:
        logger.info("subagents: report findings block was not JSON (%s)", exc)
        return None
    if not isinstance(payload, dict):
        logger.info("subagents: report findings block was not an object")
        return None
    try:
        return ReportStructure.model_validate(payload)
    except ValidationError as exc:
        logger.info("subagents: report findings block did not validate (%s)", exc)
        return None


def report_prose(text: str) -> str:
    """The report with its structured block taken out, which is what a human reads.

    The operator gets the prose and the panel the structure; the model still sees the whole
    envelope, being the one reader that can use both.
    """
    return _FINDINGS_BLOCK.sub("", text).strip()
