"""How a message crossing the link between a thread and its sub-agent is framed.

Two messages cross it, in opposite directions: a **report** coming back when a sub-agent
finishes, and a **direction** going down when the launching agent redirects one that is
still working. Both are framed here, on one shape, because they have the same problem.

Pydantic AI has one shape for a message from outside the model — ``UserPromptPart`` — so a
report rides into the launching thread as one. Left bare it would claim the operator typed
it: the model would answer as though they had, the transcript would show it as theirs, and
every later turn would replay it that way. A direction has the mirror of that problem —
a sub-agent has no operator at all, so unframed text in its thread is from nobody. So both
are wrapped, once, here.

**One envelope, both roads.** A report reaches the thread two ways — queued into a turn
that is still running, or as the prompt of a turn it wakes — and the wording is identical,
because those are the same event and nothing downstream should be able to tell which route
it took.

**Two readers, one constant.** To the model the envelope is plain framing and nothing
parses it. Coming back out, the transcript recognises it and renders a report rather than
something the operator said. That reader matches the **whole** header this module writes —
opener *and* preamble — rather than the opening tag alone, because an operator can type
angle brackets and a message of theirs relabelled as a sub-agent's is the same lie in
reverse.

**It is a label, not a credential**, and nothing may come to depend on it as one: a text
marker is forgeable by anyone who can type it, and what a forgery buys is a differently
styled bubble in the forger's own thread. The load-bearing answer to "who sent this" is
``QueuedMessage.source``, which is structural and never arrives from outside.

It lives in ``services/`` rather than beside the injection point because both of those
readers are below the agent layer, and the alternative to one home is two copies of a
marker that only works while they agree.

**A report also has a structured half**, appended inside the same envelope and defined in
the second section of this module: findings, conflicts and per-topic coverage as data, so
a fan-out of researchers can be read as a coverage map rather than as N essays. Same
reasoning for the same home — the writer, the parser and the brief that asks for it only
work while they agree on one format.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

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

#: Said to the model, not to the operator. Deliberately explicit that they have not seen
#: this — a model that thanked them for the finding would be the first sign it had gone
#: wrong — and stripped back out before the transcript shows the report.
_REPORT_PREAMBLE = (
    "A sub-agent you launched has finished and reported back. This is its report, not a "
    "message from the operator — they have not seen it and are not waiting on a reply to "
    "it. Carry on with whatever it unblocks, and tell them what matters."
)

#: The mirror, read by the sub-agent rather than by the thread that launched it. Explicit
#: about all three things a sub-agent would otherwise get wrong here: who this is from,
#: that it supersedes rather than adds to the brief, and that answering it is not a thing —
#: nobody is reading the sub-agent's thread, and its one reply is the report it ends with.
_DIRECTION_PREAMBLE = (
    "The agent that launched you has sent you this. It is not from the operator, and it "
    "changes the task you were given — take it as amending your brief, and prefer it where "
    "the two disagree. Do not reply to it; carry on with the work and report as usual."
)


def _header(opener: str, preamble: str) -> str:
    """The whole header, which is what an envelope is recognised by — opener *and*
    preamble, never the tag alone. Anyone can type an opening tag, and a message
    relabelled as a sub-agent's is the same lie as a report relabelled as the
    operator's."""
    return f"{opener}\n{preamble}\n\n"


_MESSAGE_PREAMBLE = (
    "Another agent in your delegation tree sent you this. It is not from the operator. "
    "Weigh it against your task, which it may amend if it comes from the agent that "
    "spawned you. To answer, send a message back with `agents_message`."
)

_REPORT_HEADER = _header(REPORT_OPEN, _REPORT_PREAMBLE)
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
    return _wrap(_REPORT_HEADER, REPORT_CLOSE, text)


def report_body(text: str) -> str | None:
    """The report inside an envelope, or ``None`` when this is not one."""
    return _unwrap(_REPORT_HEADER, REPORT_CLOSE, text)


def direction_body(text: str) -> str | None:
    """The direction inside an envelope, or ``None`` when this is not one."""
    return _unwrap(_DIRECTION_HEADER, DIRECTION_CLOSE, text)


def message_envelope(sender: str, text: str) -> str:
    """A message from the agent ``sender`` names, framed as one."""
    return _wrap(_MESSAGE_HEADER, MESSAGE_CLOSE, f"From `{sender}`:\n{text.strip()}")


def message_body(text: str) -> tuple[str, str] | None:
    """The sender and text inside a message envelope, or ``None`` when this is not one."""
    body = _unwrap(_MESSAGE_HEADER, MESSAGE_CLOSE, text)
    if body is None:
        return None
    match = _SENDER_LINE.match(body)
    if match is None:
        return None
    return match.group("sender"), body[match.end() :].strip()


# --- The report's structured half --------------------------------------------------
#
# A report was prose and nothing else, which is fine for the *model* reading it — prose is
# what a model is for — and useless for everything else. The operator asks three questions
# of a fan-out of researchers that prose cannot answer without being read end to end: what
# was actually established, where the sources disagree, and which corners of the question
# nobody covered. Those are the same three questions for every research thread, which is
# what makes them a shape rather than a summary.
#
# **Beside the prose, never instead of it.** The block is appended inside the same
# envelope, so the launching model reads the report exactly as it did before and a
# sub-agent that emits no block — or a malformed one — costs a panel, not a report. That
# degrade is the whole reason this parses leniently and logs rather than raising: a
# report is the only thing that survives a sub-agent, and losing one to a stray comma
# would be the worst possible trade.

#: Bounds the JSON block inside the report. Its own markers rather than a ``` fence: a
#: research report quotes code and quotes markdown, and a delimiter a source can contain is
#: a delimiter that eventually cuts the report in the wrong place.
FINDINGS_OPEN = "<subagent-findings>"
FINDINGS_CLOSE = "</subagent-findings>"

_FINDINGS_BLOCK = re.compile(
    re.escape(FINDINGS_OPEN) + r"\s*(?P<json>.*?)\s*" + re.escape(FINDINGS_CLOSE),
    re.DOTALL,
)

Confidence = Literal["high", "medium", "low"]

#: How well a topic was covered. ``none`` is a real and useful answer — a topic nobody
#: reached is exactly what a coverage map exists to show, and a sub-agent that omitted the
#: row instead would make a gap indistinguishable from a topic it never thought of.
Depth = Literal["none", "thin", "adequate", "deep"]


class _Lenient(BaseModel):
    """Unknown keys are dropped rather than rejected. The author is a model writing JSON
    by hand at the end of a long task; a field it invented is not a reason to throw the
    report's structure away."""

    model_config = ConfigDict(extra="ignore")


class ReportSource(_Lenient):
    """Where a sub-agent read something. ``url`` for a page, ``ref`` for a passage out of
    the operator's own corpus — the same split :class:`core.citations.Citation` carries,
    because a report naming its sources and the run stream naming them have to be talking
    about the same things."""

    url: str | None = None
    ref: str | None = None
    title: str | None = None


class Finding(_Lenient):
    """One thing the sub-agent established, and what it rests on.

    ``sources`` is what makes this a finding rather than an assertion: the count of
    *independent* sources behind a claim is the thing a reader most wants and most rarely
    gets, and it is a count only if the sources are listed.
    """

    statement: str
    confidence: Confidence = "medium"
    sources: list[ReportSource] = []
    #: Which topic of the investigation this belongs to, matched by name against
    #: :class:`TopicCoverage.topic`. Free text — the topics are the launching agent's own
    #: words, and a vocabulary fixed here could not name them.
    topic: str | None = None


class ConflictPosition(_Lenient):
    """One side of a disagreement, and who is on it."""

    claim: str
    sources: list[ReportSource] = []


class Conflict(_Lenient):
    """A question the sources answer differently.

    First-class rather than synthesized away, which is the point: a summary that picks a
    side silently is indistinguishable from sources that agreed, and the disagreement is
    usually the most informative thing the research found. ``assessment`` is where the
    sub-agent says which it finds more credible *and why* — it may take a side, as long as
    the taking is visible.
    """

    question: str
    positions: list[ConflictPosition] = []
    assessment: str | None = None


class TopicCoverage(_Lenient):
    """How far the investigation actually got on one topic."""

    topic: str
    depth: Depth = "thin"
    source_count: int = 0
    #: What is missing, in the sub-agent's own words. A named gap is worth more than a
    #: confident sentence covering it.
    gaps: list[str] = []


class ReportStructure(_Lenient):
    """The report as data. Every list may be empty — a sub-agent that found nothing, hit
    no contradiction and covered one topic is reporting honestly, not failing."""

    findings: list[Finding] = []
    conflicts: list[Conflict] = []
    coverage: list[TopicCoverage] = []
    #: Questions the sub-agent could not settle, including any it was not equipped to.
    unresolved: list[str] = []


#: What a reporting sub-agent is told about the block, appended to the briefs of the ones
#: whose reports are worth a panel. Deliberately explicit that the prose still comes first:
#: a sub-agent that replaced its report with JSON would have obeyed this and destroyed the
#: thing the launching agent actually reads.
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
    """``structure`` as the block a report ends with — the writer's half of the parser
    below, so tests and any future non-model producer share one format."""
    payload = structure.model_dump(mode="json", exclude_none=True)
    return f"{FINDINGS_OPEN}\n{json.dumps(payload, indent=2)}\n{FINDINGS_CLOSE}"


def report_structure(text: str) -> ReportStructure | None:
    """The structured block inside a report, or ``None`` when there isn't a usable one.

    Takes the report with or without its envelope, because both readers have one in hand
    at different points and neither should have to unwrap first.

    **Never raises.** A sub-agent that emitted no block, emitted broken JSON, or emitted
    something that is not an object at all costs the operator a panel and nothing else —
    the prose report is untouched and is what the launching agent reads either way. The
    failure is logged because a *persistently* unparseable block is a brief that needs
    rewording, and that is invisible otherwise.
    """
    match = _FINDINGS_BLOCK.search(text)
    if match is None:
        return None
    try:
        payload = json.loads(match.group("json"))
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
    """The report with its structured block taken out — what a human reads.

    The operator gets the prose and the panel gets the structure, and neither should be
    shown the other's copy: a card ending in forty lines of JSON is a card nobody reads to
    the end. The *model* still sees the whole envelope, block included, which is
    deliberate — it is the one reader that can use both.
    """
    return _FINDINGS_BLOCK.sub("", text).strip()
