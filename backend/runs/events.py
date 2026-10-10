"""Pillar II — the event protocol (the backend↔frontend contract).

The frozen v1 typed event union. Framing: SSE, each frame's ``id:`` is the
per-run monotonic ``seq`` and ``data:`` is the flat JSON envelope
``{type, seq, ts, ...payload}``. Naming is ``entity.event``, dot.lowercase —
past-tense verbs for things that happened, ``delta``/``progress`` for streams.

Producers build a typed body (e.g. ``AnswerDelta(text=...)``); the Run stamps
``seq``/``ts`` at emit time and wraps it in an :class:`Event`.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from core.compaction_sections import SummarySection, summary_sections

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 1


def now_utc() -> datetime:
    return datetime.now(UTC)


class _Body(BaseModel):
    model_config = ConfigDict(frozen=True)


# --- Run ---------------------------------------------------------------------
class RunStarted(_Body):
    type: Literal["run.started"] = "run.started"
    run_id: str
    kind: str
    protocol_version: int = PROTOCOL_VERSION


class ContextThresholds(_Body):
    """Where a filling context window stops being unremarkable and starts being a
    problem — the two boundaries the gauge changes colour on.

    **Operator-tunable**, because the point at which the remaining room stops being
    enough is a property of how someone works rather than of the model: a thread of
    long tool results can spend the last quarter of a window in a single turn, while a
    short back-and-forth has a dozen turns left at the same fullness. A fixed boundary
    is therefore either early enough to be noise for one operator or late enough to be
    useless for the other.

    Fractions, not percentages — the same 0-1 quantity :attr:`ContextWindow.fraction`
    and auto-compaction's own threshold already carry, so nothing here has to agree on
    a second convention.

    ``warn`` strictly below ``alert`` is an invariant, not a preference: equal
    boundaries make the amber band unreachable, and inverted ones walk the gauge
    backwards through severity as it fills. Enforced here so that the one construction
    path is also the one check."""

    warn: float = Field(gt=0, lt=1)
    alert: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def _ordered(self) -> ContextThresholds:
        if self.warn >= self.alert:
            raise ValueError("the warn threshold must be below the alert threshold")
        return self


#: The boundaries in force when the operator hasn't moved them. 75/90 leaves roughly a
#: turn or two of warning at typical turn sizes before the window is genuinely tight.
#:
#: **These do not relate to the fold, and the gauge says so rather than pretending they
#: do.** They were chosen when compaction fired at 0.95, so both sat below it and the ring
#: reddened while there was still something a fold could do. Compaction now fires at 0.80
#: (the trigger measures the turn *about to run*), which puts `alert` **above** the fold
#: point: on a thread with folding on, the window is emptied before it can ever reach 0.90,
#: so the red band is effectively unreachable. That is not a bug in either number — a
#: warning and a fold want different moments, and the operator can move them
#: independently, which is the whole reason they are two dials. It does mean the gauge has
#: to show where the fold is (:class:`FoldPoint`) instead of leaving the operator to infer
#: it from a band that never lights.
DEFAULT_CONTEXT_THRESHOLDS = ContextThresholds(warn=0.75, alert=0.9)


class FoldPoint(_Body):
    """Where conversation compaction will fold this thread, and whether it is armed.

    Rides on :class:`ContextWindow` rather than being fetched separately, because it is
    only meaningful against the same window the fullness is measured in — and because it is
    **per-thread**: the operator's global threshold, with that conversation's on/off
    override folded in. A client reading the global setting would draw the wrong mark on a
    thread whose folding the operator switched off.

    Deliberately *not* part of the severity derivation. ``level`` stays a function of
    ``warn``/``alert`` alone: an alert is a notification and a fold is an act on the
    thread, the right moment for the two differs, and collapsing them into one number would
    mean every future adjustment to one silently retuned the other.

    ``active`` false still carries a ``fraction``: a thread with folding paused should show
    where the fold *would* fire, dimmed, rather than dropping the mark and leaving the
    operator with no sense of the room they are spending."""

    fraction: float = Field(gt=0, le=1)
    active: bool

    @classmethod
    def of(cls, fraction: float, *, active: bool) -> FoldPoint | None:
        """A fold mark for ``fraction``, or ``None`` when there is no sensible mark to draw.

        **Constructed through here rather than directly, because this is a readout and a
        readout must not be able to stop the thing it describes.** The bounds above are
        real — a fold at 0 would fire on an empty thread and one above 1 could never fire —
        but the threshold reaching them is not always a validated value: ``_float_or``
        bounds what the *settings store* holds and then falls back to
        ``Settings.auto_compact_threshold``, which is a plain unbounded float. An operator
        running with ``ODYSSEUS_AUTO_COMPACT_THRESHOLD=0`` would otherwise have a gauge
        decoration raise inside the orchestrator — killing every turn — and 500 every
        conversation load, over a mark on a bar.

        So an out-of-range threshold draws no mark, which is the honest reading: there is
        no share of the window this thread meaningfully folds at. The misconfiguration is
        still wrong and still changes what ``should_compact`` does; it just stops being
        fatal on the way to the screen."""
        if not 0 < fraction <= 1:
            logger.warning(
                "no fold mark: auto_compact_threshold %r is outside (0, 1]", fraction
            )
            return None
        return cls(fraction=fraction, active=active)


class ContextSegment(_Body):
    """One line item inside a group — a tool category, a contributor to the standing
    brief, a class of message content.

    **Present only when it weighs something.** The segment list is not a fixed roster
    with zeros in it: a thread that has called no tools carries no `tool_results` row, a
    catalog with no MCP servers connected carries no `external` row, and both appear the
    moment they start costing the window. That is the difference between a readout the
    operator scans and a form they have to read — the rows that are there are the rows
    that matter.

    ``id`` is a slug, not a label: the tool category as the operator's own settings page
    names it, the instruction provider's slug, or the message class. The wording is the
    client's — a readout row is presentation, and the backend has no business choosing
    sentence case. ``count`` is the population behind the figure where one exists (tools
    in a category, `null` elsewhere), because "22k of schemas" and "22k of schemas across
    68 tools" lead to different decisions."""

    id: str
    group: Literal["brief", "tools", "messages"]
    tokens: int
    count: int | None = None


class ContextComposition(_Body):
    """What the occupied part of the window is actually holding.

    Two resolutions of one measurement. The three totals are exhaustive by construction —
    they are scaled to sum to :attr:`ContextWindow.used` (see ``services.context_budget``)
    — so the operator can read them as a whole rather than wondering what the remainder
    is. ``segments`` itemises those same tokens without adding any: each segment belongs
    to exactly one group, and a group's segments sum to its total.

    The itemisation is what makes the readout answer the *next* question. "Tools are 40%
    of your window" is where the three-way split stops and where the operator's actual
    decision starts — which tools, and can they be switched off. Empty when a
    measurement could reach the totals but not the detail, so the coarse reading never
    depends on the fine one.

    Every figure is an **estimate anchored to the provider's total**: the split is ours,
    measured from what we assembled, because no provider reports one. Surfaces render
    these with a `~`."""

    system: int  # the standing brief: instructions + system prompt
    tools: int  # every tool name, description and JSON schema handed to the model
    messages: int  # the conversation itself
    segments: tuple[ContextSegment, ...] = ()


class ContextWindow(_Body):
    """How full a model's context window is after a turn — the single owner of
    the fullness derivation and its severity thresholds. Built by
    :meth:`from_usage` and emitted both live (run metrics) and on load
    (conversation detail), so clients render one shape from either source."""

    used: int  # tokens occupying the window: last response's prompt + generation
    window: int  # the model's context window
    fraction: float  # used / window, clamped to 0–1
    level: Literal["nominal", "warn", "alert"]
    # What `used` is made of, when it could be measured. Null on a thread whose turns all
    # predate the measurement, and on a cold load of one — the split is captured while a
    # request is being assembled, and a reload has no request to look at.
    parts: ContextComposition | None = None
    # Where this thread's fold will fire, and whether it is armed. Null when the caller
    # had no compaction policy to hand — which is a different statement from "folding is
    # off": off is `active=False` with the fraction still on it. A client draws no mark
    # for null and a dimmed one for inactive.
    fold: FoldPoint | None = None

    @classmethod
    def from_used(
        cls,
        used: int | None,
        window: int | None,
        thresholds: ContextThresholds = DEFAULT_CONTEXT_THRESHOLDS,
        parts: ContextComposition | None = None,
        fold: FoldPoint | None = None,
    ) -> ContextWindow | None:
        """Derive the window state from the context footprint (``used``), or None
        when there's no ceiling to measure against or no footprint was reported.

        ``thresholds`` are the operator's severity boundaries; the default is what a
        caller with no settings store to consult gets. The *level* is resolved here and
        travels on the wire, so that the gauge, the overflow warning, and anything else
        keying off severity read one boundary rather than each re-deriving it — the
        client renders a level, it never decides one."""
        if not window or used is None:
            return None
        fraction = min(1.0, used / window)
        level = (
            "alert"
            if fraction >= thresholds.alert
            else "warn"
            if fraction >= thresholds.warn
            else "nominal"
        )
        return cls(used=used, window=window, fraction=fraction, level=level, parts=parts, fold=fold)


class LastRequestUsage(_Body):
    """What the thread's most recent model request cost, on its own.

    Spelled out rather than named ``RequestUsage``, which is what Pydantic AI calls the
    provider-reported usage this is partly built from — two types with one name, one of
    them ours and one of them the library's, is a confusion that would land the first time
    a module needed both.

    Everything else on :class:`RunMetrics` is conversation-cumulative, which is the right
    default for a readout under the composer and the wrong one for the two questions this
    answers: *which* endpoint served that last request, and how much of its prompt the
    provider had already cached. A cumulative figure cannot answer either — a fallback
    chain's second model and a cold cache both disappear into a running total.

    ``route`` is the model that actually answered, prefixed by its provider when one is
    reported (``openai:qwen3-32b``). On a fallback chain that is not necessarily the model
    the thread is bound to, which is exactly why it is worth reporting.

    Every token field is **absent, never zero**, on the same rule the frame below it
    follows: null means the provider said nothing, and most OpenAI-compatible and local
    endpoints say nothing about caching at all."""

    route: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None

    # The two figures behind `prefill_tokens_per_second` below. Kept as plain fields, with
    # the arithmetic as a computed property, on the same pattern the frame's throughput
    # figure follows: the client renders a rate, it never derives one.
    #
    # `prefill_tokens` is deliberately **not** `input_tokens`. A local server routinely
    # reports `input_tokens=0` meaning "not measured", which is the whole reason the
    # composition readout estimates a footprint at all — so the numerator here is that
    # measured-or-estimated prompt size, which is the one figure available on every endpoint.
    prefill_tokens: int | None = None
    #: Time to the first content of any kind on that response — connect, queue and prefill,
    #: which is why the rate below is *apparent*.
    prefill_ms: int | None = None

    @computed_field
    @property
    def prefill_tokens_per_second(self) -> float | None:
        """The **apparent** prompt-processing rate — prompt tokens over time to first token.

        Apparent, and the word is load-bearing. Time to first token is connect plus queue
        plus prefill, and on a single local server shared with the utility role the queue
        term is exactly what a background review or a summarizer puts there. So a figure
        that halves between two turns of the same thread may mean the prompt cache missed,
        or may mean something else was mid-generation when this request arrived — this
        cannot tell them apart, and claiming a "prefill rate" flatly would invite reading it
        as the former.

        It is still the most useful single number for the question it serves, because the
        alternative is nothing: no endpoint this codebase talks to reports its own prefill
        time in a standard field, and llama.cpp's own `timings.prompt_ms` — which is the
        unconfounded measurement — arrives in a non-standard block outside the OpenAI shape.
        Where that is read, prefer it over this.

        Null whenever either term is missing or non-positive: a response with no first token
        (a bare tool call the timer saw no content from), a thread whose prompt size could
        not be estimated, or a sub-millisecond round trip whose division we would not trust.
        """
        if not self.prefill_tokens or not self.prefill_ms or self.prefill_ms <= 0:
            return None
        return self.prefill_tokens / (self.prefill_ms / 1000)


class RunMetrics(_Body):
    """What the thread has cost so far — the readout under the composer.

    **Conversation-cumulative, not per-run.** Every count here spans the whole active
    path: the run seeds them from the conversation's persisted totals and accumulates
    its own on top. A per-run frame was what this used to be, and it made the line
    unreadable — the numbers reset to zero at the start of every turn, so the one
    moment the operator most wants to know what a long thread has spent is the moment
    the readout says nothing.

    Absent, never zero. A null field means *not measured* — a provider that reports no
    cache tokens, a turn that streamed no content to time. Zero means measured as zero.
    The distinction is the whole reason these are nullable, and it is what lets the UI
    omit a segment rather than assert a flattering 0%.
    """

    type: Literal["run.metrics"] = "run.metrics"
    steps: int = 0
    tool_calls: int = 0
    # Completed exchanges on the active path — what a reader calls a "turn", where
    # `steps` counts the model round-trips a turn took internally.
    turns: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    # Prompt tokens the provider served from its own cache. **Provider-reported, so
    # null on every endpoint that doesn't report it** — most OpenAI-compatible and
    # local servers. This is the one number here we can't measure ourselves, and a 0
    # would read as "your caching is broken" rather than "nobody said".
    cache_read_tokens: int | None = None
    # Wall-clock, measured by us around our own node iteration so it means the same
    # thing on every endpoint (see `agent/timings.py`). `llm_ms` is the full model
    # round-trip including connect and queue — the wait the operator actually sat
    # through — not a claim about the provider's inference time.
    llm_ms: int | None = None
    tool_ms: int | None = None
    # Summed time-to-first-content and the number of responses that produced any, kept
    # apart so the average survives being added to another run's totals. A single
    # pre-averaged field could not be accumulated without drifting.
    ttft_ms_total: int | None = None
    ttft_samples: int = 0
    # The model's context window, when known — the ceiling the derived `context`
    # measures against. Null when the endpoint declares none.
    context_window: int | None = None
    # The context footprint after this turn: the *last* model response's prompt +
    # generation (what the next turn carries forward). Deliberately NOT the run's
    # cumulative input/output above — those sum every internal model request and
    # would overstate fullness several-fold on tool-calling or multi-turn runs.
    context_used: int | None = None

    # The operator's severity boundaries, seeded onto the Run at turn start and read
    # by `context` below. Deliberately **not serialized**: it is an input to the
    # derivation, not part of the readout, and putting it on the wire would invite a
    # client to re-derive the level it is already being handed.
    context_thresholds: ContextThresholds = Field(default=DEFAULT_CONTEXT_THRESHOLDS, exclude=True)

    # This thread's fold point, seeded onto the Run beside the thresholds and for the same
    # reason. Also **not serialized**: it reaches the client on `context` below, where it
    # sits against the window it is a fraction of, rather than twice on one frame.
    context_fold: FoldPoint | None = Field(default=None, exclude=True)

    # How that footprint splits across the standing brief, the tool schemas and the
    # conversation. Measured during the turn (the tool definitions are only knowable while
    # a request is being assembled), so it rides on the frame rather than being derived
    # from it.
    context_parts: ContextComposition | None = None

    # The last model request on its own — the route it took and what the provider's cache
    # did with it. Sits beside the cumulative figures rather than replacing any of them:
    # the two answer different questions and the totals above are what the composer's line
    # reads. Null on a thread that has never produced a response. Additive to v1; no bump.
    last_request: LastRequestUsage | None = None

    @computed_field
    @property
    def context(self) -> ContextWindow | None:
        """The context-window fullness after this turn — null when unmeasurable
        (no window, or no footprint). Clients render it; they never derive it."""
        return ContextWindow.from_used(
            self.context_used,
            self.context_window,
            self.context_thresholds,
            self.context_parts,
            self.context_fold,
        )

    @computed_field
    @property
    def cache_hit_ratio(self) -> float | None:
        """Share of prompt tokens the provider served from cache, 0–1. Null when the
        provider reports no cache figure, or before any prompt tokens are counted."""
        if self.cache_read_tokens is None or not self.input_tokens:
            return None
        return min(1.0, self.cache_read_tokens / self.input_tokens)

    @computed_field
    @property
    def ttft_avg_ms(self) -> int | None:
        """Mean time to first content across the responses that produced any."""
        if not self.ttft_samples or self.ttft_ms_total is None:
            return None
        return round(self.ttft_ms_total / self.ttft_samples)

    @computed_field
    @property
    def output_tokens_per_second(self) -> float | None:
        """Generation throughput — output tokens over the time actually spent *generating*.

        That is ``llm_ms`` minus the time to first token, and the subtraction is the
        whole correctness of this figure rather than a refinement of it. ``llm_ms`` is
        the full round-trip: connect, queue, process the prompt, then generate. Only the
        last of those produces tokens. On a local model with a long thread the prefill
        can be most of the wall-clock — a 20s TTFT in front of 5s of generation is
        ordinary — so dividing by the total reported something like a fifth of the real
        rate, and got slower the longer the conversation grew even though the model was
        decoding at exactly the same speed.

        TTFT is precisely the non-generating head of the request, which is why it is the
        right thing to subtract. Both are summed over the same responses, so this is the
        thread's mean decode rate, not the last turn's.

        Still measured against model time and not elapsed time: the operator's wait also
        includes tool execution and their own thinking between turns, and dividing by
        that would report a rate that falls the longer they leave the tab open."""
        if not self.output_tokens or self.llm_ms is None:
            return None
        generating_ms = self.llm_ms - (self.ttft_ms_total or 0)
        # Non-positive means the arithmetic has nothing to say: a thread whose responses
        # were all TTFT and no generation, or timings recorded before this was measured.
        # Absent beats a number derived from a division we don't trust.
        if generating_ms <= 0:
            return None
        return self.output_tokens / (generating_ms / 1000)


class RunEnded(_Body):
    """The closing frame for a run that reached an *expected* end.

    A run closes with exactly one of `run.ended` or `run.error`, never both — the
    terminal frame has two shapes because the two carry different payloads, and a
    failure has a message and an exception kind where an expected end has an outcome
    and a detail. Readers treat either as end-of-stream (`isTerminal` on the client).
    `error` is deliberately absent from ``outcome`` for that reason: it is not an
    outcome this frame ever carries. Both are preceded by `run.metrics`.
    """

    type: Literal["run.ended"] = "run.ended"
    outcome: Literal["done", "blocked", "cancelled"]
    detail: str | None = None


class RunError(_Body):
    """The closing frame for a run that failed. See :class:`RunEnded` — these two are
    the same terminal position in the protocol, not a frame plus an extra."""

    type: Literal["run.error"] = "run.error"
    message: str
    kind: str | None = None


# --- Step --------------------------------------------------------------------
class StepStarted(_Body):
    type: Literal["step.started"] = "step.started"
    index: int
    title: str | None = None


class StepCompleted(_Body):
    type: Literal["step.completed"] = "step.completed"
    index: int


# --- Content (the reasoning/answer split) ------------------------------------
class ThinkingDelta(_Body):
    type: Literal["thinking.delta"] = "thinking.delta"
    text: str


class AnswerDelta(_Body):
    type: Literal["answer.delta"] = "answer.delta"
    text: str


# --- Tools (full args + results inline, not summaries) -----------------------
class ToolStarted(_Body):
    """A tool call began. ``parent_tool_call_id`` is set when a script made the call — the
    ``tool_call_id`` of the ``run_code`` call it was made from — and absent for a call the
    model made directly; the same field rides ``tool.completed`` and ``tool.failed``, so a
    client nests the whole lifecycle under the script that ran it. Additive to v1."""

    type: Literal["tool.started"] = "tool.started"
    tool_call_id: str
    name: str
    args: dict[str, Any] = Field(default_factory=dict)
    parent_tool_call_id: str | None = None


class ToolProgress(_Body):
    type: Literal["tool.progress"] = "tool.progress"
    tool_call_id: str
    elapsed_s: float | None = None
    partial: str | None = None


class ToolImage(BaseModel):
    """An image a tool handed back for the model to look at — a screenshot, today.

    It rides the call's completion rather than arriving as a turn of its own: no
    provider accepts an image inside a tool result, so Pydantic AI moves it into a
    user-role part, and that wire detail must not reach the operator as a message they
    appear to have sent. The work log is where the call is, so it is where the picture
    of what the call saw belongs."""

    media_type: str
    data: str  # base64, no data-URI scheme — the renderer adds it


class ToolCompleted(_Body):
    type: Literal["tool.completed"] = "tool.completed"
    tool_call_id: str
    name: str
    result: Any = None
    images: list[ToolImage] = Field(default_factory=list)
    parent_tool_call_id: str | None = None


class ToolFailed(_Body):
    type: Literal["tool.failed"] = "tool.failed"
    tool_call_id: str
    name: str
    error: str
    parent_tool_call_id: str | None = None


# --- View (the conversation's one versioned output surface) ------------------
# The View is one canvas with a history of **versions** to compare plus an optional
# live **head**. A version is one ``view.snapshot`` (the captured workspace tree +
# how it previews); ``view.live`` / ``view.live.stopped`` are the interactive head
# overlaid on the latest version.
class ViewLive(_Body):
    """The agent started (or replaced) the View's live head — a running server.
    ``url`` is a token-gated proxy path on this same API origin
    (``/previews/{token}/…``) that streams the server's HTTP and WebSocket traffic
    out of the sandbox.

    Frontend contract: mount it as ``<iframe src={url}>`` with
    ``sandbox="allow-scripts allow-forms allow-popups"`` — deliberately **without**
    ``allow-same-origin``, so the framed (model-generated) app runs in an opaque
    origin and cannot act as the operator against the API. The token in the path is
    the credential, so no auth header is needed and relative subresources/WebSockets
    resolve automatically. ``url`` already carries the entry path when one was given,
    so it renders the page rather than a directory listing. Additive to v1; no bump."""

    type: Literal["view.live"] = "view.live"
    conversation_id: str
    url: str  # "/previews/{token}/<entry>"
    title: str | None = None
    command: str  # the server command, for display
    port: int  # the in-container port it listens on


class ViewLiveStopped(_Body):
    """The View's live head was torn down (explicitly via close_view, or reaped with
    its idle session); the frontend drops the live iframe for this conversation."""

    type: Literal["view.live.stopped"] = "view.live.stopped"
    conversation_id: str


class ViewSnapshot(_Body):
    """A new **version** of the conversation's View — minted by a ``show``. It captures
    the agent's sandbox tree (the version's code, browsable + diffable via
    ``/views/snapshots/{snapshot_id}/…``) and how it previews: ``preview_artifact_id``
    + ``preview_kind`` point at the captured-bytes preview of a ``show(file=…)`` (fetch
    them from ``/views/{preview_artifact_id}/content``), or both are null for a
    live/auto preview (a running head, or the frontend auto-picks an entry HTML page).
    Additive to v1; no bump."""

    type: Literal["view.snapshot"] = "view.snapshot"
    conversation_id: str
    snapshot_id: str
    title: str | None = None
    created_at: datetime
    files_changed: int
    summary: str  # compact change tally, e.g. "+2 ~1 −0"
    preview_kind: str | None = None  # "html" | "image" | "text" | "other" | None
    preview_artifact_id: str | None = None


# --- Conversation ------------------------------------------------------------
class ConversationTitled(_Body):
    """The chassis named a freshly-created conversation from its first exchange,
    so the operator never has to. The title is persisted too; the frontend reveals
    it with a typing animation. Emitted mid-run (before ``run.ended``) so a still-
    open stream carries it. Additive to v1; no bump."""

    type: Literal["conversation.titled"] = "conversation.titled"
    conversation_id: str
    title: str


#: Why a fold happened. ``threshold`` is the prelude's projected-pressure trigger,
#: ``overflow`` the in-turn recovery after a provider refused an over-long request, and
#: ``manual`` the operator pressing "compact now". One vocabulary for both compaction
#: events, so a client that learns the word on one frame reads it on the other.
CompactionReason = Literal["threshold", "overflow", "manual"]


class CompactionStarted(_Body):
    """A fold is about to be summarized — emitted *before* the summarizer call, which is
    the one part of compaction that takes real time (a whole model pass over the thread).

    Without it the operator watches a thread sit silent for as long as the summary takes
    with nothing said, and the only frame that ever mentioned compaction was the one that
    announced it as already done. It also refreshes the inactivity watchdog (every
    ``Run.emit`` touches the activity clock) — the fold itself then holds the watchdog open
    with ``Run.keepalive``, so a long summary can't be read as a stalled run. Additive to
    v1; no bump."""

    type: Literal["compaction.started"] = "compaction.started"
    conversation_id: str
    reason: CompactionReason
    # What the fold is about to cover: how many messages, and their coarse
    # `estimate_tokens` size — the same proxy `conversation.compacted` reports against, so
    # the "before" figure the progress block shows is the one the divider settles on.
    messages: int
    tokens_estimate: int


class CompactionDelta(_Body):
    """A piece of the summary as the summarizer writes it.

    ``compaction.started`` says a fold has begun and ``conversation.compacted`` says it is
    done; between them sits the part that actually takes the time, and until this event
    there was nothing in it. On a local endpoint that is tens of seconds of a throbber with
    a count beside it, which tells the operator that something is happening and never what.

    **This is the model's working, not the checkpoint.** The text arrives raw: not stripped
    of a leaked ``<think>`` block, not merged with the anchors a previous fold carried
    forward, and not fenced. Those are done to the settled summary, and two of them cannot
    be done to a fragment at all. So a client renders these as a live view that is replaced
    when ``conversation.compacted`` lands with the real thing — never stores one, and never
    treats one as the summary the model will actually read."""

    type: Literal["compaction.delta"] = "compaction.delta"
    conversation_id: str
    text: str


class CompactionFailed(_Body):
    """A fold that began did not land — the closing frame for a ``compaction.started`` that
    will never be followed by ``conversation.compacted``.

    Without it a client holding a live "compacting" section, filled with the streamed
    summary, had nothing to tell it the work was abandoned: the section sat there until the
    run ended and a reload showed no trace of it, because the backend had never recorded a
    compaction. On this frame the client drops the section (and its streamed text) and may
    show ``message``. Emitted for every failure of a fold that had something to do — never
    for a fold that found nothing to fold, which also emits no ``compaction.started``.
    ``message`` is a short operator-readable cause, not a log line. Additive to v1; no
    bump."""

    type: Literal["compaction.failed"] = "compaction.failed"
    conversation_id: str
    reason: CompactionReason
    message: str


class ConversationCompacted(_Body):
    """The thread's earlier turns were folded into a summary before this turn ran,
    because its context footprint had reached the operator's threshold. Nothing was
    deleted — the transcript keeps every turn; this marks where the *model's* replayed
    view narrows to ``summary`` plus the turns after it. Emitted mid-run (before the
    answer streams) so a live client can drop the divider in as it happens, and
    persisted as its own message so a reload renders the same thing. Additive to v1;
    no bump."""

    type: Literal["conversation.compacted"] = "conversation.compacted"
    conversation_id: str
    # Why the fold happened, in the same words `compaction.started` used. Defaulted so a
    # replayed frame from before this field existed still parses as what it was.
    reason: CompactionReason = "threshold"
    # The checkpoint message the summary is stored on — the node the client renders
    # the divider against, and the same id a cold read returns.
    message_id: str
    summary: str
    # How many messages the summary stands in for, so the divider can say so without
    # the client counting anything itself.
    messages_compacted: int
    # What the fold cost, in coarse tokens: `tokens_before` over the messages that were
    # folded, `tokens_after` over the summary that replaced them. Both are the same
    # `estimate_tokens` text-only proxy the compaction trigger itself measures with — not
    # a provider's usage report — so a client should render them as approximate ("~62k →
    # ~4k"), never as billing figures. `services/conversation_view.py` recomputes the
    # identical three values for the cold-read compaction row, so a live divider and the
    # one a reload draws say the same thing.
    tokens_before: int = 0
    tokens_after: int = 0
    # The rendered turn the divider follows. A live client addresses turns, not tree
    # nodes, so the backend resolves the position rather than leaving the client to
    # approximate it and land somewhere a reload disagrees with. Null => append.
    after_message_id: str | None = None

    @computed_field
    @property
    def sections(self) -> list[SummarySection]:
        """``summary`` split into the roster sections a divider renders.

        Derived rather than passed in, for the same reason :attr:`RunMetrics.context` is: a
        second field carrying a parse of a field already on the frame is a second thing to
        keep in step, and the two disagreeing is a bug nobody sees until a checkpoint reads
        wrong. ``services/conversation_view.py`` calls the *same* function on the *same*
        stored text, so the divider a live client draws and the one a reload draws are the
        same divider."""
        return summary_sections(self.summary)


class ConversationLinked(_Body):
    """This turn opened another conversation, and the operator should know why.

    A thread that spawns a thread is the one case where work leaves the surface the
    operator is watching: the new one appears in their session list a moment later, and
    without this it appears with no account of where it came from. ``relation`` names what
    the new thread is *for* rather than what created it — today only ``"research"``, and
    stated as a string rather than an enum because a second kind of linked thread should
    be a new value, not a protocol change.

    Emitted by the tool that opened it, mid-run, so a live client sees it as it happens.
    Additive to v1; no bump."""

    type: Literal["conversation.linked"] = "conversation.linked"
    conversation_id: str
    relation: str
    # What the new thread was opened to do — its title, which for a research thread is the
    # question. Null only if it was opened without one.
    title: str | None = None


# --- Context -----------------------------------------------------------------
#: The most text one injection puts on the wire. The token figure is always the whole
#: block's, so a capped ``text`` costs the operator the tail of a long file and nothing
#: else — while an uncapped one would put a 60KB instruction file into the run's replay
#: buffer on every turn that reads it, to be re-sent in full to every reattaching client.
INJECTED_TEXT_LIMIT = 8_000


class ContextInjected(_Body):
    """Something the chassis put in front of the model that nobody in the conversation
    wrote — a project's instruction files, the skill catalog, the plan reminder, the
    date.

    The gauge already says these cost the window; this says *when they arrived and what
    they said*. Those are different questions, and only the second one answers "why did
    the model act as if it had been told that" — which, on a thread where the operator
    reads every message and still cannot account for the model's behaviour, is the whole
    question. Emitted as the turn is assembled, so it lands in the work log ahead of the
    work it shaped.

    ``contributor`` is the same slug :class:`ContextSegment` uses for the standing
    brief's rows, and deliberately so: the popover's "Skill catalog · ~4k" and this
    event's row are the same block seen from two distances, and a client that named them
    differently would make the operator work out that they are one thing. The wording
    stays the client's, for the reason stated there.

    ``placement`` is where in the request it landed — ``instructions`` at the head
    (re-sent every turn, never retained in history) or ``prompt`` at the tail of the
    turn's own user message (volatile content kept out of the cacheable prefix). It is on
    the wire because it is the difference between a block that costs the operator a cache
    invalidation and one that does not.

    ``tokens`` is the coarse estimate the rest of the readout uses, over the **whole**
    block — never over the possibly-truncated ``text``. Additive to v1; no bump."""

    type: Literal["context.injected"] = "context.injected"
    contributor: str
    placement: Literal["instructions", "prompt"]
    tokens: int
    # The resolved block, capped at `INJECTED_TEXT_LIMIT` characters. `truncated` says so
    # rather than leaving the operator to guess whether a file simply ends there.
    text: str
    truncated: bool = False


# --- Notices -----------------------------------------------------------------
class CitationAdded(_Body):
    """A source the turn met, and how far it got with it.

    ``key`` is what this source *is* — a URL for a page, ``source:ref`` for a corpus
    passage — and is what a client folds repeat sightings by. It is on the wire rather
    than derived because "these two rows are one source" is a claim about the sources: a
    client computing it would be a client deciding, and a web citation and a corpus one do
    not fold by the same field.

    ``engagement`` is the rung of ``listed`` < ``read`` < ``cited`` this sighting reached
    (see :class:`core.citations.Engagement`). One source may arrive on several frames —
    listed by a search, then read by a fetch — and a client keeps the highest. Before this
    every hit a tool returned was emitted identically, so "cited" meant no more than "a
    tool returned it", and the operator could not tell the page the answer rests on from
    the eight beside it in a result list.

    ``url`` is null for a corpus passage, which has a locator rather than an address —
    the one non-additive part of this body, and the reason a client must read ``kind``
    before it renders a link. ``published`` is the source's own date exactly as its
    provider reported it, never parsed here; ``retrieved_at`` is when this run read it.
    ``snippet`` is the text the source was seen through, already unfenced and capped.

    Deliberately **not** carried: any link from this source to a particular sentence of
    the answer. That link exists, but it is not a property of a source — it is a reading
    of the finished answer, produced after the turn by a second model pass and served
    from its own route (``GET /conversations/{id}/attributions``). What reaches this frame
    from it is one thing: ``engagement="cited"``, emitted once per source a claim was
    actually grounded in, which is the first honest use of that rung anywhere.
    """

    type: Literal["citation.added"] = "citation.added"
    url: str | None = None
    title: str | None = None
    key: str = ""
    kind: Literal["web", "corpus"] = "web"
    engagement: Literal["listed", "read", "cited"] = "listed"
    snippet: str | None = None
    published: str | None = None
    retrieved_at: datetime | None = None
    #: ``kind="corpus"`` only — which indexed source the passage came from, and where in it.
    source_id: str | None = None
    ref: str | None = None


class ApprovalRequired(_Body):
    """A sensitive action is parked awaiting operator approval."""

    type: Literal["approval.required"] = "approval.required"
    tool_call_id: str
    name: str
    args: dict[str, Any] = Field(default_factory=dict)
    summary: str
    # Plain-language description of what the action does and its effect, so the
    # operator can judge it without reading the raw arguments — set when the tool
    # supplies one (the host-execution path requires it). Additive to v1; no bump.
    explanation: str | None = None


class QuestionOption(_Body):
    """One answer offered for a question."""

    label: str
    description: str | None = None


class QuestionSpec(_Body):
    """One question the operator is being asked."""

    question: str
    options: list[QuestionOption] = Field(default_factory=list)
    multi_select: bool = False


class QuestionAsked(_Body):
    """The turn is parked on the operator answering, not on them permitting.

    Its own event rather than an `approval.required` with a different shape, because the
    two ask for different things and are answered with different things. An approval is a
    yes or a no about an action already decided on; a question has no default, no safe
    side, and comes back carrying a *value* that becomes the tool's result. Folding one
    into the other would have made `approved` meaningless for half its uses.

    One event per call, carrying every question in it: the model asks for what it needs in
    one go, and the operator answers it in one go. Additive to v1; no bump."""

    type: Literal["question.asked"] = "question.asked"
    tool_call_id: str
    questions: list[QuestionSpec] = Field(default_factory=list)


class AnsweredQuestionOut(_Body):
    """One question and what the operator said to it, as structure rather than as prose.

    The tool's *result* is a rendered paragraph, because that is what the model reads. A
    client asked to draw a card from it would have to parse that paragraph back apart —
    and a renderer that parses prose is a renderer that breaks the day the wording is
    improved. So the same answer rides here already taken apart, built from the parked
    call's own arguments and the operator's replies.

    ``selections`` is empty when they wrote instead of choosing, and ``text`` is null when
    they only chose; both are filled when they did both."""

    question: str
    selections: list[str] = Field(default_factory=list)
    text: str | None = None


class QuestionAnswered(_Body):
    """The operator answered a parked ``ask_user`` call.

    The counterpart to :class:`QuestionAsked`, and it exists for the same reason that one
    does: the question arrives as structure, so the answer has to as well, or the card the
    client drew from the question has nothing but the tool result's prose to complete
    itself with. Emitted per answered call, never per question.

    What it carries is derived from the **parked call's arguments** plus the replies the
    server validated against them — never from labels the client sent — so the answer in
    the transcript cannot say something the question never offered. Additive to v1; no
    bump."""

    type: Literal["question.answered"] = "question.answered"
    tool_call_id: str
    answers: list[AnsweredQuestionOut] = Field(default_factory=list)


class ReviewStarted(_Body):
    """An action at the Auto level is being ruled on in the operator's place.

    Auto's proposition is that the operator's approvals are given for them. The only thing
    that makes that acceptable is that they can see it happening and read afterwards what
    was decided and why — so the review announces itself before it runs, rather than a
    tool call simply appearing to have been made.

    ``summary`` is the action's extracted worst case, in the same words the reviewer is
    judging and the operator can read. ``detail`` is the act's own content where the tool
    has some worth reading — a delegated task, a program, the reason given for opening a
    credential — and null where there is none, which is most tools. ``reach`` is how far a
    shell command *declared* it needs to go — the worktree, the network, or the host — and
    is null for every kind of act that declares nothing, which is a different fact from
    declaring the widest reach. Additive to v1; no bump."""

    type: Literal["review.started"] = "review.started"
    tool_call_id: str
    name: str
    summary: str
    detail: str | None = None
    reach: Literal["workspace", "network", "host"] | None = None


class ReviewCompleted(_Body):
    """How the review ruled, on the three axes it ruled on.

    ``decision`` is the outcome the run then took — ``allow`` ran the call without a
    prompt, ``ask`` parked it for the operator anyway. ``block``, which refused the call
    outright, is no longer produced: an act the reviewer judged unrecoverable parks like
    anything else it will not clear, since that is the one act the operator most needs put
    in front of them. The word stays in the vocabulary because it is already written into
    the stored events of threads reviewed before that changed. It
    carries deliberately more than the outcome: ``stage`` says whether the structural judge
    or a model settled it, ``tier`` says on which ground when the judge did, ``fenced``
    whether an OS fence was there to hold the command to what it declared, and the three
    axes say what the model saw. An operator reading only "allowed" learns nothing they can
    act on; one reading "cleared structurally, fenced to the worktree" can tell an
    over-permissive rule from a well-judged call.

    The axes are null when the model stage never ran — the judge cleared it, or nothing
    was available to review with — and ``tier`` is null in the mirror case, whenever the
    model settled it. Additive to v1; no bump."""

    type: Literal["review.completed"] = "review.completed"
    tool_call_id: str
    name: str
    decision: Literal["allow", "ask", "block"]
    stage: Literal["judge", "reviewer"]
    reason: str
    tier: Literal["read", "sandbox", "workspace"] | None = None
    #: Whether this host could confine the command at all. False is why an ordinary
    #: contained command reached a model reviewer, and it is the operator's to fix.
    fenced: bool = False
    risk: Literal["low", "high", "too_destructive"] | None = None
    authorization: Literal["explicitly_no", "neutral", "explicitly_yes"] | None = None
    correctness: str | None = None


#: Who a queued message came from. ``operator`` is somebody typing while a run is going;
#: ``subagent`` is a sub-agent's report, delivered into the thread that launched it; and
#: ``parent`` is the same link read the other way — the launching agent redirecting a
#: sub-agent that is still working.
#:
#: All three ride the same road deliberately — the injection point already guarantees a
#: queued message never interrupts an in-flight model stream, which is exactly what a
#: report and a direction both need — but they must not *read* the same. A report rendered
#: as the operator's own words is a transcript that lies about who said what, to the reader
#: and to the model.
#:
#: This is the load-bearing answer to "who sent this". The envelope
#: (``services/subagents/report.py``) is a label anyone who can type angle brackets could
#: forge; this is structural and never arrives from outside.
MessageSource = Literal["operator", "subagent", "parent"]


class MessageQueued(_Body):
    """A message arrived while this run was still executing; it is queued for injection at
    the run's next model-request boundary. ``text`` rides inline so a reattaching client
    can rebuild the pending bubble purely from replay.

    ``source`` says whose message it is. It defaults to ``operator``, so a client that
    predates sub-agents reads every frame exactly as it did before. Additive to v1; no
    bump."""

    type: Literal["message.queued"] = "message.queued"
    message_id: str
    text: str
    source: MessageSource = "operator"


class MessageEdited(_Body):
    """The operator rewrote a queued message's text before the run consumed it.
    ``text`` is the full replacement (not a delta), inline for the same reason as
    ``message.queued``'s: a reattaching client rebuilds the pending bubble purely
    from replay. Additive to v1; no bump."""

    type: Literal["message.edited"] = "message.edited"
    message_id: str
    text: str


class MessageWithdrawn(_Body):
    """The operator withdrew a queued message before the run consumed it.
    Additive to v1; no bump."""

    type: Literal["message.withdrawn"] = "message.withdrawn"
    message_id: str


class MessageHeld(_Body):
    """A queued message was held back from the drain, or released again.

    Held is not withdrawn: the message stays in the queue, in its place, and goes to the
    model the moment it is released. What it buys is the interval while the operator has
    the message open in an editor — the run would otherwise inject the draft they are
    still rewriting, and an edit landing a moment later would have nowhere to go.

    The state is the backend's, not the editor's, which is why it is a frame rather than a
    client-side flag: a second tab, a reload, or a client attaching mid-run all have to see
    the same queue the run does. Additive to v1; no bump."""

    type: Literal["message.held"] = "message.held"
    message_id: str
    #: True when the hold went on, False when it came off. Both directions ride one event
    #: rather than two, because a client renders the message's state, not the transition.
    held: bool


class MessageInjected(_Body):
    """A queued message was handed to the model (emitted in drain order). From
    here on the message is part of the turn and will persist as a normal user
    message. Additive to v1; no bump."""

    type: Literal["message.injected"] = "message.injected"
    message_id: str
    #: Repeated from the queue frame rather than looked up, so a client attaching after the
    #: queue frame scrolled out of its replay window still knows whose message landed.
    source: MessageSource = "operator"


class TasksUpdated(_Body):
    """The agent's task list changed. Carries the **whole** list, not a delta: the stream
    is replayable from any seq, so full state is idempotent on replay and needs no ordering
    rules, and the list is small enough that the bytes don't matter. Each item is
    ``{id, content, status, active_form}`` with status one of pending/in_progress/
    completed/cancelled. Additive to v1; no bump."""

    type: Literal["tasks.updated"] = "tasks.updated"
    items: list[dict]


class PlanUpdated(_Body):
    """The written plan a Plan-level turn produced, or its status changing.

    Distinct from :class:`TasksUpdated` in the way the two concepts are: that one is the
    running checklist, this is the document the operator is being asked to approve. Carries
    the whole plan for the same reason — full state replays idempotently — and a
    ``revision`` that increments on every resubmission, which is what lets the panel treat
    a revised plan as a new arrival rather than a redraw of the old one.

    ``status`` is one of pending/approved/revising/denied. Additive to v1; no bump."""

    type: Literal["plan.updated"] = "plan.updated"
    title: str
    body: str
    steps: list[str]
    status: str
    revision: int


class PermissionChanged(_Body):
    """The thread's permission level changed from inside the run.

    Load-bearing rather than informational. The level is the one fact the client both
    *holds* and *sends back* — it rides every message and the backend persists it against
    the conversation — so a client that did not hear about a level the agent changed would
    write the stale one straight back over it on the operator's next message. Emitted by
    the two tools that move it (``tools/plan.py``); an operator-chosen level needs no event
    because the client is where it came from. Additive to v1; no bump."""

    type: Literal["permission.changed"] = "permission.changed"
    level: str
    #: Why, in one line, for the transcript — "entering plan mode", "plan approved".
    reason: str


#: The ``subagent.*`` family that used to live here is gone with the blocking delegation
#: that emitted it. It existed because a delegation was a multi-minute silence *inside*
#: one tool call, so the parent's own stream had to narrate a child it was holding open.
#: A sub-agent is now a run of its own: it has a stream, a transcript and a register row,
#: and the panel reads those directly. Relaying a second copy of them onto whichever
#: parent run happened to be open would be a feed that stops the moment the parent's turn
#: ends — which is most of a sub-agent's life.


class LimitNotice(_Body):
    type: Literal["limit.notice"] = "limit.notice"
    # "steps" | "tool_calls" | "tokens" | "time" | "loop" | "verify" | "context" | "search"
    # ("context" = the model's context window was exceeded; the run stops, it isn't degraded.
    # "search" was the deep-research pipeline's two-empty-rounds abort; nothing emits it now
    # that research is an ordinary thread, and it stays in the vocabulary because removing a
    # value a client already handles is a narrowing, which this protocol does not do.)
    limit: str
    message: str
    # The stop marker this notice announces, when the bound also blocked the turn — the
    # same string the turn persists as its `blocked_reason`. It is here so the toast and
    # the marker on the stopped turn can offer the *same* remedy: two context stops share
    # one `limit` value but not one answer, and prose is not something a client can key on.
    detail: str | None = None


EventBody = Annotated[
    RunStarted
    | RunMetrics
    | RunEnded
    | RunError
    | StepStarted
    | StepCompleted
    | ThinkingDelta
    | AnswerDelta
    | ToolStarted
    | ToolProgress
    | ToolCompleted
    | ToolFailed
    | CitationAdded
    | ViewLive
    | ViewLiveStopped
    | ViewSnapshot
    | ConversationTitled
    | CompactionStarted
    | CompactionDelta
    | CompactionFailed
    | ConversationCompacted
    | ConversationLinked
    | ContextInjected
    | ApprovalRequired
    | QuestionAsked
    | QuestionAnswered
    | ReviewStarted
    | ReviewCompleted
    | MessageQueued
    | MessageEdited
    | MessageWithdrawn
    | MessageHeld
    | MessageInjected
    | TasksUpdated
    | PlanUpdated
    | PermissionChanged
    | LimitNotice,
    Field(discriminator="type"),
]


@dataclass(frozen=True, slots=True)
class Event:
    """A stamped event: the producer's body plus the run-assigned seq/ts."""

    seq: int
    ts: datetime
    body: BaseModel

    def envelope(self) -> dict[str, Any]:
        """Flat ``{type, seq, ts, ...payload}`` dict — the on-the-wire shape."""
        data = self.body.model_dump(mode="json")
        data["seq"] = self.seq
        data["ts"] = self.ts.isoformat()
        return data

    def sse(self) -> str:
        """One SSE frame: ``id:`` carries seq for Last-Event-ID resume."""
        payload = json.dumps(self.envelope(), separators=(",", ":"))
        return f"id: {self.seq}\ndata: {payload}\n\n"
