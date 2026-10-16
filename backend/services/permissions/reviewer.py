"""The model stage — one structured call that scores an action on three named axes.

What reaches here is what the deterministic stage would not vouch for (``judge.py``):
an act whose worst case is real, or one whose worst case could not be read at all. The
question is no longer "is this provably harmless" — it is "did the operator ask for this,
and is it worth what it could cost", which is a judgement about a conversation and
therefore a model's to make.

**Three axes, not one score.** A single "is this ok" number collapses two independent
facts — how bad the act is, and whether it was wanted — and a model asked for the
collapsed answer will trade one against the other silently. Scored apart, the trade is
made *here*, in code someone can read: ``risk`` is a property of the act alone,
``authorization`` a property of the conversation alone, and ``correctness`` is the
free-text observation that catches the case neither number does — the right kind of act
aimed at the wrong thing.

**Two rules copied from LM Studio's design, both load-bearing:**

- **The prompt carries the rubric and never the passing score.** A reviewer told what
  clears the bar optimises for clearing it; a reviewer told only what the words mean
  answers the question it was asked. The combination lives in ``decide.py``, and nothing
  about it is stated here or in the prompt.
- **The transcript excludes tool results.** Everything the model has read from a file, a
  page or an MCP server is content someone else wrote, and a reviewer that saw it could be
  talked into an approval by text the attacker chose — an approval, note, of the very call
  that untrusted text asked for. So the reviewer sees the operator's own messages and the
  assistant's own prose, and nothing that came back from a tool. It is the same posture
  every other ingress in this codebase takes, applied to the one call whose output is a
  permission.

  **Including the tool results a compaction folded.** A conversation compaction writes its
  summary back as a *user* message (``services/conversations.py``), and that summary is
  built from the whole thread, tool returns and all (``agent/summarize.py``) — so a page
  the agent read once reaches this file wearing the one label the rubric treats as
  authorising. It is dropped by the marker it is written with. The cost is real and is the
  right way round: after a compaction the reviewer reads only the turns since, which is
  less to authorize on, and less authorization parks.

  **And the per-turn context riding on the operator's own prompt.** The chassis appends
  volatile context — today the agent's own task list — to the tail of the turn's user
  prompt (``agent/prelude.py``), so a *model-authored* string arrives inside the one
  message this file labels ``operator``. Only the leading item of a prompt is the
  operator's (:func:`_prompt_text`); a plan the model wrote after reading a poisoned page
  is not an authorization however the transport arranged it.

- **The turn's opening request is always in it.** Authorization is a question about what
  the operator asked for, and the answer is in the message that started the turn — the
  first thing a window counted from the end drops once the turn has run a few tools. So it
  is taken off the turn boundary and kept, and the window is spent on what came after
  (:func:`review_transcript`).

Even so, the transcript that *is* shown goes inside an untrusted fence: the assistant's
prose is downstream of everything it has read, so it can carry an injected argument
forward in its own words. Fencing it means such an argument arrives as something the
reviewer reads about, not as something it is told.

**Two things follow from that, and both are about who wrote which byte.** The prompt is
split down that line rather than by topic: what *this process* derived — the tool's name,
the paths the walk found, whether the command reaches the network, what could not be read
— stands in the clear, and every byte the *model* authored — the action's summary, and so
the command inside it — sits inside the fence with the conversation. And the conversation
is handed over as **JSON entries, not labelled lines**: a transcript rendered as
`Operator: …` is a format any message can write, so an assistant turn (or a pasted page
quoted in one) could open a second "Operator:" line and hand itself the one label the
rubric treats as authorising. A role that is a JSON field cannot be forged by the text in
the field beside it. Both fences share one nonce and one preamble, so the reviewer is told
the rule once and can still see where each block begins.

**Degradation is fail-closed and lives at the caller.** This module returns ``None`` for
every failure it can have — no model bound, a timeout, an unparseable answer — and never
a lenient verdict standing in for one. What ``None`` *means* is ``decide.py``'s to say,
and it says park.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelRequest, TextPart, UserPromptPart
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings

from core.compaction_sections import is_checkpoint
from core.untrusted import new_nonce, untrusted_fence, untrusted_preamble
from prompts.utility import REVIEW_INSTRUCTIONS
from services.permissions.capability import Capability

logger = logging.getLogger(__name__)

#: How much of the thread the reviewer reads, counted from the end in **prose entries** —
#: the operator's and the assistant's own words — rather than in messages. Counting
#: messages was the bug it is named after: a tool round trip is two of them, so after six
#: tool calls the window held nothing but tool traffic, the prompt said "the operator has
#: said nothing", and every high-risk act parked. What the reviewer needs from the thread
#: is what was *said*, and this is a budget over exactly that.
TRANSCRIPT_ENTRIES = 12

#: Per-message cap, so one pasted file in a user message cannot crowd out the ten turns
#: around it. Generous enough that an ordinary request survives whole.
MESSAGE_CHARS = 2_000


#: Whether the operator asked for an act, as the reviewer read the thread. Named because
#: ``decide.py`` computes with it — a standing grant can stand in for the reviewer's answer
#: — and a second spelling of the three words there would be a second place to change.
type Authorization = Literal["explicitly_no", "neutral", "explicitly_yes"]

#: How far the operator's standing conversation grant reaches over the call being ruled on.
#: ``"none"`` is no grant; ``"command"`` is one recorded from an approval, naming the act it
#: was ticked under; ``"tool"`` is their deliberate wider pick, a yes to everything that
#: tool does in the thread. Named here, beside the answer it stands in for, because
#: ``decide.py`` computes with both and the store that records a grant
#: (``services/approval_grants.py``) is a layer *above* this package — so the vocabulary
#: has to live at the bottom or be spelled twice.
type GrantWidth = Literal["none", "command", "tool"]


class ReviewVerdict(BaseModel):
    """One reviewer's scores. Typed rather than prose for the reason every structured
    utility call in this codebase is: a successful injection can move a label, and can
    never issue an instruction."""

    #: What the act could cost. ``too_destructive`` is unrecoverable, and it overrules
    #: everything this model can say: no authorization it reads out of the turn clears it.
    #: The one thing that does is the operator's own whole-tool grant, which is not part of
    #: this verdict and is not visible from here — see ``decide.py``.
    risk: Literal["low", "high", "too_destructive"] = "high"
    #: Whether the operator asked for it. Only the operator's own messages count.
    authorization: Authorization = "neutral"
    #: What about the act does not match the request, in a sentence. None ⇒ it matches.
    correctness: str | None = Field(default=None, max_length=400)


@dataclass(frozen=True)
class TranscriptEntry:
    """One turn of the conversation as the reviewer may read it.

    A role and a text, kept apart, because the prompt renders them as JSON: the label is a
    field the message's own text cannot reach, which is the whole difference between "the
    operator said this" and "something claims the operator said this".
    """

    role: Literal["operator", "assistant"]
    text: str


@dataclass(frozen=True)
class ReviewRequest:
    """What a reviewer is given: an action's worst case, and the thread that led to it."""

    capability: Capability
    #: The recent conversation, already filtered to user and assistant prose
    #: (:func:`review_transcript`). Empty is legitimate — a stateless turn has no thread.
    transcript: Sequence[TranscriptEntry] = ()
    #: The fence token both untrusted blocks carry, and the one the preamble names.
    #:
    #: **A field rather than something the prompt mints**, because a turn's deferred calls are
    #: reviewed as a *batch* over one transcript (``agent/gating.py``), and the transcript is
    #: the largest thing in the prompt. Minted inside the prompt it differed per call, so N
    #: reviews of one batch shared their prefix up to the first byte of the nonce and the
    #: server re-read the same several kilobytes of conversation N times.
    #:
    #: The default still mints one, so a request built anywhere else is fenced with a token
    #: nothing has seen. What sharing narrows is the nonce's *scope* — per call to per batch —
    #: and that is safe for the reason the fence works at all: the token is unpredictable to
    #: the content being fenced, and every call in a batch is fenced in the same one message's
    #: worth of prose. It is never shared across turns, across threads or over time.
    nonce: str = field(default_factory=new_nonce)


#: The stage as a function, so the engine holds a reviewer rather than a model and a test
#: can substitute one without a model at all. Returns None on any failure (see the module
#: docstring); the caller turns that into a park.
type Reviewer = Callable[[ReviewRequest], Awaitable[ReviewVerdict | None]]


class TurnBoundary(Protocol):
    """Where the current turn's own messages begin, as far as this module needs to know.

    The engine's ``agent.history.TurnStart`` is what is passed, and it is deliberately not
    imported: ``services`` sits below ``agent``, and the only thing wanted here is the one
    question that object already answers. Structural typing keeps the dependency pointing
    the way the layer map says it must.
    """

    def slice(self, messages: list[ModelMessage]) -> list[ModelMessage]:
        """``messages`` from the turn's boundary on."""
        ...  # pragma: no cover — a Protocol body, never executed


def review_transcript(
    messages: Sequence[ModelMessage],
    *,
    turn_start: TurnBoundary | None = None,
    limit: int = TRANSCRIPT_ENTRIES,
    chars: int = MESSAGE_CHARS,
) -> tuple[TranscriptEntry, ...]:
    """The thread as the reviewer may see it: the operator's requests and the assistant's
    prose, in order, and nothing else.

    Four kinds of part are dropped and each for its own reason. **Tool returns** are
    content from outside the conversation and are the injection vector this whole design
    is arranged around. **Tool calls** are the model's own requests, and the one being
    judged is already described far more precisely by its capability. **Thinking** is the
    model's private argument for what it is about to do, which is exactly the material a
    reviewer should not be weighing when deciding whether the *operator* asked for it.
    And a **compaction summary** is a user-shaped message the operator never wrote: a
    utility model's fold of the earlier thread, tool returns included, which would
    otherwise carry every one of them back in wearing the operator's own role.

    **What survives the trim is chosen twice over, and the first choice is the important
    one.** ``turn_start`` is where this turn's own messages begin, so the operator's
    request(s) that opened it are taken out first and always kept — the reviewer's single
    most load-bearing input is the thing it is being asked to authorize *against*, and it
    is also the entry a window counted from the end loses first, since a turn that has run
    fourteen tools has pushed it far out of reach. Everything else is the most recent
    ``limit`` entries of prose.

    **The order that comes back is chronological, because the prompt says it is.** Only an
    opening entry the window has already *dropped* is put back, and it is put back in
    front: having fallen out of a window counted from the end, it is by construction older
    than everything left in one. An opening entry the window still holds keeps its own
    place. Hoisting it instead — which is what deduplicating opening-first did — moved the
    turn's request ahead of strictly older messages, so a superseded instruction read as
    the last thing the operator said.
    """
    entries = _prose(messages, chars)
    opening = (
        tuple(
            entry
            for entry in _prose(turn_start.slice(list(messages)), chars)
            if entry.role == "operator"
        )
        if turn_start is not None
        else ()
    )
    window = entries[-limit:] if limit > 0 else ()
    kept: dict[TranscriptEntry, None] = dict.fromkeys(
        entry for entry in opening if entry not in window
    )
    kept.update(dict.fromkeys(window))
    return tuple(kept)


def _prose(messages: Sequence[ModelMessage], chars: int) -> tuple[TranscriptEntry, ...]:
    """Every message that carries somebody's own words, as one entry each, in order."""
    entries: list[TranscriptEntry] = []
    for message in messages:
        if isinstance(message, ModelRequest):
            text = "\n".join(
                _prompt_text(part)
                for part in message.parts
                if isinstance(part, UserPromptPart) and not _is_compaction_summary(part)
            ).strip()
            if text:
                entries.append(TranscriptEntry("operator", text[:chars]))
        else:
            text = "\n".join(
                part.content for part in message.parts if isinstance(part, TextPart)
            ).strip()
            if text:
                entries.append(TranscriptEntry("assistant", text[:chars]))
    return tuple(entries)


def _is_compaction_summary(part: UserPromptPart) -> bool:
    """Whether this user-shaped message is a compaction checkpoint rather than a request.

    Recognized by the opening tag the compaction writes, a marker rather than a heuristic.
    An operator who types that tag themselves loses their message from the reviewer's
    view, which costs them a park and nothing else.
    """
    return is_checkpoint(_prompt_text(part))


def _prompt_text(part: UserPromptPart) -> str:
    """The **operator's own words** in a user part, and nothing else that rides in it.

    A list content is not a multi-part message the operator typed — it is their one prompt
    with everything the chassis appended to it behind: attachment markers, image bytes, and
    the per-turn tail context (``agent/prelude.py``). That last one is the reason this is a
    positional read rather than a join. The tail context includes the agent's **own** task
    list, written by the model through ``tasks_write``, and joining the list would hand
    it to the reviewer inside the entry labelled ``operator`` — the exact forgery the JSON
    roles exist to prevent, except with the chassis supplying the label, so nothing would
    have to be forged. A task the model wrote after reading a poisoned page would then read
    as the operator's own instruction, on the turn's opening request, which is pinned into
    every review of that turn.

    The operator's prompt is at position 0 or it is not in the part at all: everything the
    prelude adds is appended. So that is what is read, and a non-string there (a bare image
    turn) contributes nothing.
    """
    if isinstance(part.content, str):
        return part.content
    first = part.content[0] if part.content else ""
    return first if isinstance(first, str) else ""


def review_prompt(request: ReviewRequest) -> str:
    """The reviewer's one message: everything anyone wrote, then the structural facts.

    The split is by **author**, not by topic. What stands in the clear is what this
    process derived — the tool's name, the paths the grammar walk found, whether the
    action reaches the network, and what could not be read at all. What goes inside the
    fence is every byte a model produced: the action's summary (which, for a shell action,
    is the command), its ``detail`` where the tool has one, and the conversation. Putting
    the command in the clear was the older shape and the wrong one — a command is a string
    the model chose, and a reviewer reading it in the clear is reading instructions from
    the thing it is reviewing. The same holds for a projected argument: quoting a delegated
    task or a stated reason is how the reviewer rules on it, and quoting is not trusting.

    The facts are JSON-encoded even though the *labels* are ours to trust, because their
    values are not: a path is a word the model wrote, and a word with a newline in it
    could otherwise write a line of its own. That covers the tool's own name too — an
    operator's MCP server names its tools, not this catalog.

    **The order is batch-invariant first, per-call last, and that is a cache decision.** A
    turn's deferred calls are reviewed together over one transcript, so the preamble and the
    conversation are byte-identical across every review in the batch while the described
    action and the measurements are not. Leading with the shared part is what lets an engine
    serve the second and later reviews' prompts from the prefix it already processed for the
    first; the older shape put the per-call facts first and shared nothing past the rubric.
    Worth several kilobytes per extra review, and the whole of it depends on the batch
    sharing one ``nonce`` — which is why that is a field on the request rather than minted
    here.

    Nothing about what may be trusted moves with it. The rubric still arrives ahead of
    everything on the ``instructions=`` seam, so it cannot be displaced by anything in this
    message; the preamble that says both blocks are data still precedes both of them; and
    the measurements now sit *closest to the answer*, which if anything favours them —
    they are the ground the verdict is judged on, and the fenced prose they override is
    what the reviewer reads first and weighs last.
    """
    capability = request.capability
    # One nonce and one preamble across both fences: the rule is the same rule, and the
    # reviewer that has been told it once does not read it better for being told twice.
    nonce = request.nonce
    lines = [untrusted_preamble(nonce), ""]
    if request.transcript:
        conversation = [{"role": entry.role, "text": entry.text} for entry in request.transcript]
        lines.append("The conversation so far, oldest first:")
        lines.append(untrusted_fence(json.dumps(conversation), nonce, source="conversation"))
    else:
        lines.append("There is no conversation to read: the operator has said nothing.")
    lines += ["", "The action, as the model described it:"]
    # `detail` is the act's own content where the tool has some worth reading — a delegated
    # task, a program, the reason given for opening a credential (`capability.py`). It is a
    # second JSON field rather than more of the summary, and absent rather than null where
    # there is none: a reviewer reading `"detail": null` learns nothing and pays for it.
    described = {"summary": capability.summary}
    if capability.detail is not None:
        described["detail"] = capability.detail
    lines.append(untrusted_fence(json.dumps(described), nonce, source="tool-call"))
    lines += ["", "What this system measured for itself:", f"Tool: {json.dumps(capability.tool)}"]
    for label, values in (
        ("Reads", capability.reads),
        ("Writes", capability.writes),
        ("Sets in the environment", capability.env_writes),
        ("Reaches outside the workspace", capability.escapes),
        ("Could not be fully read", capability.unbounded),
    ):
        if values:
            lines.append(f"{label}: {json.dumps(list(values))}")
    # The sandbox line first, because the rubric's `low` turns on it: an interpreter's
    # program is not bounded by anything here, but the sandbox it runs in bounds what the
    # program can reach, and the reviewer has no other way to tell that call from one on
    # the operator's own machine.
    lines.append(
        f"Runs inside the conversation's sandbox: {'yes' if capability.sandboxed else 'no'}"
    )
    # And the network fact is always the grammar walk's, which is why the label says what
    # was *seen written* rather than what the action can reach. The grammar walk is the
    # only producer (widening a workspace's egress is its own approval-gated act), so one
    # producer means one label; wording that described the sandbox's egress would report
    # a walk result as a switch.
    #
    # Seeing no address is deliberately not the same claim as "reaches nothing": `git
    # push`, `npm publish` and `scp host:/path` all write none, so the label says what was
    # seen and the rubric says in the same breath that seeing none proves nothing.
    lines.append(f"Names a network address: {'yes' if capability.network else 'no'}")
    if capability.reach is not None:
        # The one model-authored value that stands in the clear, and it can: it is an
        # enumerated word this process re-derived from the call, not free text, so there is
        # no string here for an author to write a line of prose into. It belongs beside the
        # paths rather than inside the fence because the reviewer's question about it is
        # structural — does what this command names match what it said it needed.
        lines.append(f"Declared reach: {capability.reach}")
    return "\n".join(lines)


def make_utility_reviewer(
    model: Model,
    *,
    model_settings: ModelSettings | None = None,
    timeout_s: float = 30.0,
    max_tokens: int = 1024,
) -> Reviewer:
    """A reviewer over the cheap utility model, bounded by a timeout it cannot exceed.

    The timeout is not an optimisation. A review runs inside a live turn with the
    operator watching, and a reviewer that hangs would hold a run open indefinitely on a
    call nobody has been asked about — worse than the park it was trying to avoid. So a
    slow model produces ``None``, and ``None`` means park.

    The agent and its settings are built **here** rather than inside the closure. Nothing
    about either varies per call, and building an agent means deriving a JSON schema from
    :class:`ReviewVerdict` — paid once per reviewer, where one reviewer already serves a
    whole batch of deferred calls (``agent/gating.py``), instead of once per call.
    """
    agent = Agent(model, output_type=ReviewVerdict, instructions=REVIEW_INSTRUCTIONS)
    settings: ModelSettings = {
        "max_tokens": max_tokens,
        "temperature": 0.0,
        **(model_settings or {}),
    }

    async def review(request: ReviewRequest) -> ReviewVerdict | None:
        try:
            async with asyncio.timeout(timeout_s):
                result = await agent.run(review_prompt(request), model_settings=settings)
        except TimeoutError:
            logger.warning(
                "auto review timed out for %s — parking instead", request.capability.tool
            )
            return None
        except Exception:  # every failure degrades to a park, never to an allow
            logger.warning(
                "auto review failed for %s — parking instead",
                request.capability.tool,
                exc_info=True,
            )
            return None
        return result.output

    return review
