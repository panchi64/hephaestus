"""Conversation-cumulative run metrics and the last request's own usage."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, computed_field

from .base import _Body
from .context import (
    DEFAULT_CONTEXT_THRESHOLDS,
    ContextComposition,
    ContextThresholds,
    ContextWindow,
    FoldPoint,
)


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
