"""Wall-clock instrumentation for a turn — how long the model took, how long its
tools took, and how long the operator waited for the first token.

**Measured here, on our side of the wire, and never read off a provider.** The
numbers have to mean the same thing whether the turn ran on Anthropic, on an
OpenAI-compatible endpoint, or on a local engine — and providers disagree about
what they report, when, and whether they report it at all. A stopwatch around our
own node iteration is the one measurement every endpoint can be held to, so that
is what this is: `time.monotonic` deltas around the two graph nodes the
translator already walks.

That choice sets what the numbers include, and the inclusion is deliberate.
``llm_ms`` is the full round-trip — connect, queue, generate, stream — because
that is the wait the operator actually sat through; it is not a claim about the
provider's own inference time, which we cannot see. ``ttft_ms`` is measured to
the *first emitted part of any kind* — reasoning and tool calls included, not just
answer text. On a thinking model the first thing to arrive is a thinking delta, and
timing to the first answer token instead would report the model's entire reasoning
pass as latency.

That breadth is also what makes throughput correct downstream. TTFT is the
non-generating head of a request (connect, queue, prefill), so the decode rate is
output tokens over ``llm_ms - ttft_ms``. A response that reported no first token
would contribute its whole prefill to the generating side of that subtraction, which
is why even a bare tool call — which this module never otherwise sees — has to mark
one. See ``RunMetrics.output_tokens_per_second``.

The one number here that isn't a stopwatch is the cache hit — that can only come
from provider-reported usage (``cache_read_tokens``), so it is absent, never
zero, for the many endpoints that don't report it. See ``RunMetrics``.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, field


@dataclass(slots=True)
class ResponseTiming:
    """What one model round-trip cost, in milliseconds.

    One of these per ``ModelRequestNode`` — which is one model response, and so one
    persisted assistant message row. Keeping the grain at the response (rather than
    a running per-conversation total) is what lets the stats stay correct across the
    message *tree*: a rewind or a version switch changes which responses are on the
    active path, and a total summed from that path follows, where a counter would go
    on reporting time spent down a branch the operator has left.
    """

    #: The full model round-trip: connect, queue, generate, stream.
    llm_ms: int
    #: Time to the first content part of any kind, or None if the response
    #: produced no content at all (a bare tool call with no preamble).
    ttft_ms: int | None
    #: Wall-clock spent executing the tool calls *this* response asked for. Zero
    #: when it asked for none. Attributed to the response that requested them,
    #: because that is the message row the time is persisted on.
    tool_ms: int = 0


@dataclass
class TurnTimer:
    """Collects a turn's per-response timings as the translator walks the graph.

    Deliberately a passive collector with no knowledge of runs, events or storage:
    the translator drives it, the engine reads ``responses`` off it, and the store
    stamps those onto message rows. ``clock`` is injectable so the tests can assert
    exact durations instead of sleeping.
    """

    clock: Callable[[], float] = time.monotonic
    responses: list[ResponseTiming] = field(default_factory=list)

    def _ms(self, since: float) -> int:
        return max(0, round((self.clock() - since) * 1000))

    @contextmanager
    def model_request(self) -> Generator[Callable[[], None]]:
        """Time one model round-trip, yielding the callback that marks first content.

        The callback is idempotent — the translator calls it on every content part
        because it cannot know which one is first, and only the first call counts.
        The timing is appended on exit *whether or not the body raised*: a turn that
        stops on a usage bound, a loop guard or a cancel still spent the time it
        spent, and dropping it would make the run's totals quietly understate a
        turn the operator waited through.
        """
        started = self.clock()
        first: float | None = None

        def mark_first_token() -> None:
            nonlocal first
            if first is None:
                first = self.clock()

        try:
            yield mark_first_token
        finally:
            self.responses.append(
                ResponseTiming(
                    llm_ms=self._ms(started),
                    ttft_ms=None if first is None else max(0, round((first - started) * 1000)),
                )
            )

    @contextmanager
    def tool_calls(self) -> Generator[None]:
        """Time a batch of tool executions, onto the response that requested them.

        A ``CallToolsNode`` always follows the ``ModelRequestNode`` whose response
        asked for the calls, so the batch belongs to ``responses[-1]``. If there is
        no such response the time is dropped rather than misfiled — that ordering is
        the library's, and quietly inventing a row to hang it on would be worse than
        losing a measurement.
        """
        started = self.clock()
        try:
            yield
        finally:
            if self.responses:
                self.responses[-1].tool_ms += self._ms(started)


@dataclass(frozen=True, slots=True)
class TimingTotals:
    """A set of response timings summed — the shape both the live run frame and the
    cold-load aggregate report. ``ttft_samples`` rides along with ``ttft_ms_total``
    so an average can be taken over the responses that actually produced content,
    and so two of these can be added without the average drifting."""

    llm_ms: int = 0
    tool_ms: int = 0
    ttft_ms_total: int = 0
    ttft_samples: int = 0

    def __add__(self, other: TimingTotals) -> TimingTotals:
        return TimingTotals(
            llm_ms=self.llm_ms + other.llm_ms,
            tool_ms=self.tool_ms + other.tool_ms,
            ttft_ms_total=self.ttft_ms_total + other.ttft_ms_total,
            ttft_samples=self.ttft_samples + other.ttft_samples,
        )


def total_timings(timings: list[ResponseTiming]) -> TimingTotals:
    """Sum per-response timings. Responses that reported no first token contribute
    their durations but not a TTFT sample."""
    return TimingTotals(
        llm_ms=sum(t.llm_ms for t in timings),
        tool_ms=sum(t.tool_ms for t in timings),
        ttft_ms_total=sum(t.ttft_ms for t in timings if t.ttft_ms is not None),
        ttft_samples=sum(1 for t in timings if t.ttft_ms is not None),
    )
