"""The composer's readout: the cumulative counts derived off a conversation's active
path, and the wall-clock the run stopwatch measures alongside them.

Two properties carry most of the weight here and neither is obvious from the code.

**Absent is not zero.** Every count that can go unreported reports null rather than 0 —
an endpoint that sends no cache figure, a thread whose responses predate the stopwatch.
A 0 would be a claim ("nothing cached", "no time spent"); null is the truth ("nobody
said"), and it is what lets the UI drop a segment instead of printing a flattering lie.

**Derived, never accumulated.** The counts are summed off the path on every read, so
they follow a rewind or a version switch for free. A running total would keep charging
the operator for a branch they walked away from.
"""

from __future__ import annotations

from pydantic_ai import ModelRequest, ModelResponse
from pydantic_ai.messages import ModelResponsePart, TextPart, ToolCallPart, UserPromptPart
from pydantic_ai.usage import RequestUsage

from agent.metrics import turn_metrics
from core.config import Settings
from runs import Run, RunStream
from runs.events import LastRequestUsage, RunMetrics
from runs.timings import ResponseTiming, TimingTotals, TurnTimer, total_timings
from services.conversations import conversation_totals


def _prompt(text: str = "hi") -> ModelRequest:
    return ModelRequest(parts=[UserPromptPart(content=text)])


def _tool_return() -> ModelRequest:
    """A request carrying a tool result — NOT the start of an operator turn."""
    return ModelRequest(parts=[])


def _response(
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read: int = 0,
    tools: int = 0,
) -> ModelResponse:
    parts: list[ModelResponsePart] = [TextPart(content="x")]
    parts.extend(ToolCallPart(f"t{i}", {}) for i in range(tools))
    return ModelResponse(
        parts=parts,
        usage=RequestUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
        ),
    )


# ── Counting the path ────────────────────────────────────────────────────────────


def test_turns_count_exchanges_and_steps_count_round_trips():
    # One operator turn that took the model three round-trips through two tool calls.
    # "1 turn, 3 steps" is the distinction the readout draws; conflating them would
    # report a single question as three exchanges.
    messages = [
        _prompt(),
        _response(tools=1),
        _tool_return(),
        _response(tools=1),
        _tool_return(),
        _response(),
    ]
    totals = conversation_totals(messages)
    assert (totals.turns, totals.steps, totals.tool_calls) == (1, 3, 2)


def test_a_mid_run_aside_is_not_a_new_turn():
    # A message sent WHILE the model works is persisted as its own request behind the
    # tool-return it was injected into. Counting bare user prompts would read that
    # aside as a whole exchange — the same trap `_is_turn_start` documents.
    messages = [
        _prompt("start"),
        _response(tools=1),
        _tool_return(),
        _prompt("actually, also…"),  # steering, mid-run
        _response(),
    ]
    assert conversation_totals(messages).turns == 1


def test_turns_count_each_real_exchange():
    messages = [_prompt(), _response(), _prompt(), _response(), _prompt(), _response()]
    assert conversation_totals(messages).turns == 3


def test_tokens_sum_across_the_whole_path():
    # Unlike the context footprint (last response only), the readout's token counts
    # are cumulative — what the thread has cost, not what it currently occupies.
    messages = [
        _prompt(),
        _response(input_tokens=100, output_tokens=10),
        _prompt(),
        _response(input_tokens=250, output_tokens=20),
    ]
    totals = conversation_totals(messages)
    assert (totals.input_tokens, totals.output_tokens) == (350, 30)


def test_unreported_tokens_are_absent_not_zero():
    # Local servers routinely leave input_tokens at 0, indistinguishable from a real
    # zero — so an unmeasured thread reports absent rather than free.
    totals = conversation_totals([_prompt(), _response()])
    assert totals.input_tokens is None
    assert totals.output_tokens is None
    assert totals.steps == 1  # the response still counts; only its tokens are unknown


def test_cache_is_absent_when_no_provider_reports_it():
    # The one figure we can't measure ourselves. Most OpenAI-compatible and local
    # endpoints never send it, and a 0 there reads as "your caching is broken".
    totals = conversation_totals([_prompt(), _response(input_tokens=100)])
    assert totals.cache_read_tokens is None


def test_cache_sums_when_the_provider_reports_it():
    messages = [
        _prompt(),
        _response(input_tokens=100, cache_read=40),
        _prompt(),
        _response(input_tokens=200, cache_read=180),
    ]
    assert conversation_totals(messages).cache_read_tokens == 220


def test_an_empty_thread_reports_nothing():
    totals = conversation_totals([])
    assert (totals.turns, totals.steps, totals.tool_calls) == (0, 0, 0)
    assert totals.input_tokens is None


# ── The stopwatch ────────────────────────────────────────────────────────────────


class _Clock:
    """A hand-cranked monotonic clock, so durations are asserted rather than slept."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_timer_records_round_trip_and_first_token():
    clock = _Clock()
    timer = TurnTimer(clock=clock)
    with timer.model_request() as first_token:
        clock.advance(2.0)
        first_token()
        clock.advance(3.0)
    assert timer.responses == [ResponseTiming(llm_ms=5000, ttft_ms=2000)]


def test_only_the_first_token_mark_counts():
    # The translator calls the mark on every content part — it can't know which is
    # first — so the callback has to be idempotent.
    clock = _Clock()
    timer = TurnTimer(clock=clock)
    with timer.model_request() as first_token:
        clock.advance(1.0)
        first_token()
        clock.advance(4.0)
        first_token()
        first_token()
    assert timer.responses[0].ttft_ms == 1000


def test_a_response_with_no_content_reports_no_ttft():
    # A bare tool call with no preamble streams nothing to time.
    clock = _Clock()
    timer = TurnTimer(clock=clock)
    with timer.model_request():
        clock.advance(1.5)
    assert timer.responses[0].ttft_ms is None
    assert timer.responses[0].llm_ms == 1500


def test_time_is_kept_even_when_the_turn_stops():
    # A usage bound, a loop guard or a cancel still spent the time it spent. Dropping
    # it would make the totals understate a turn the operator waited through.
    clock = _Clock()
    timer = TurnTimer(clock=clock)
    try:
        with timer.model_request() as first_token:
            clock.advance(2.0)
            first_token()
            clock.advance(1.0)
            raise RuntimeError("bound tripped")
    except RuntimeError:
        pass
    assert timer.responses == [ResponseTiming(llm_ms=3000, ttft_ms=2000)]


def test_tool_time_lands_on_the_response_that_asked_for_it():
    clock = _Clock()
    timer = TurnTimer(clock=clock)
    with timer.model_request() as first:
        first()
        clock.advance(1.0)
    with timer.tool_calls():
        clock.advance(4.0)
    with timer.model_request() as first:
        first()
        clock.advance(2.0)
    assert [t.tool_ms for t in timer.responses] == [4000, 0]


def test_tool_time_with_no_response_is_dropped_not_misfiled():
    clock = _Clock()
    timer = TurnTimer(clock=clock)
    with timer.tool_calls():
        clock.advance(9.0)
    assert timer.responses == []


def test_totals_average_ttft_over_responses_that_reported_one():
    timings = [
        ResponseTiming(llm_ms=1000, ttft_ms=200, tool_ms=50),
        ResponseTiming(llm_ms=3000, ttft_ms=400),
        ResponseTiming(llm_ms=500, ttft_ms=None),  # no content — not a sample
    ]
    assert total_timings(timings) == TimingTotals(
        llm_ms=4500, tool_ms=50, ttft_ms_total=600, ttft_samples=2
    )


def test_totals_add_without_drifting_the_average():
    # Why the sum and the sample count are carried separately rather than a
    # pre-averaged field: a thread's prior turns and the live run have to add.
    prior = TimingTotals(llm_ms=1000, ttft_ms_total=900, ttft_samples=3)
    live = TimingTotals(llm_ms=500, ttft_ms_total=100, ttft_samples=1)
    combined = prior + live
    assert combined.ttft_ms_total == 1000
    assert combined.ttft_samples == 4
    assert RunMetrics(ttft_ms_total=1000, ttft_samples=4).ttft_avg_ms == 250


# ── What the frame derives ───────────────────────────────────────────────────────


def test_cache_hit_ratio_is_null_without_a_provider_figure():
    assert RunMetrics(input_tokens=1000).cache_hit_ratio is None


def test_cache_hit_ratio_reports_a_reported_zero():
    # Distinguished from absent: a provider that says "nothing cached" is believed.
    assert RunMetrics(input_tokens=1000, cache_read_tokens=0).cache_hit_ratio == 0.0


def test_cache_hit_ratio_divides_by_prompt_tokens():
    assert RunMetrics(input_tokens=1000, cache_read_tokens=940).cache_hit_ratio == 0.94


def test_throughput_excludes_the_time_before_the_first_token():
    # THE bug this guards. `llm_ms` is the whole round-trip — connect, queue, prefill,
    # generate — and only the last part makes tokens. Here 100s of model time began
    # with 80s of prompt processing, so 920 tokens were generated in 20s: 46 tok/s.
    # Dividing by the full 100s reported 9.2 — a fifth of the truth, and it would sink
    # further as the thread grew even with the model decoding at a constant rate.
    rate = RunMetrics(
        output_tokens=920, llm_ms=100_000, ttft_ms_total=80_000, ttft_samples=4
    ).output_tokens_per_second
    assert rate == 46.0


def test_throughput_measures_against_model_time_not_wall_time():
    # Tool time is excluded by construction (it isn't in llm_ms), so the rate doesn't
    # fall for a thread that spent an hour in tools or idle between turns.
    assert (
        RunMetrics(
            output_tokens=500, llm_ms=11_000, ttft_ms_total=1_000, ttft_samples=1, tool_ms=3_600_000
        ).output_tokens_per_second
        == 50.0
    )


def test_throughput_is_null_before_anything_was_generated():
    assert RunMetrics(llm_ms=5000).output_tokens_per_second is None
    assert RunMetrics(output_tokens=100).output_tokens_per_second is None


def test_throughput_is_null_when_nothing_was_left_to_generate_in():
    # All head and no tail — nothing to divide by. Absent beats a number from a
    # division we don't trust (or, worse, a negative one).
    assert (
        RunMetrics(
            output_tokens=100, llm_ms=5_000, ttft_ms_total=5_000, ttft_samples=1
        ).output_tokens_per_second
        is None
    )
    assert (
        RunMetrics(
            output_tokens=100, llm_ms=5_000, ttft_ms_total=9_000, ttft_samples=2
        ).output_tokens_per_second
        is None
    )


def test_ttft_average_is_null_without_samples():
    assert RunMetrics(ttft_ms_total=None, ttft_samples=0).ttft_avg_ms is None


# ── The apparent prefill rate ────────────────────────────────────────────────────


def test_the_apparent_prefill_rate_divides_the_prompt_by_the_wait_for_it():
    # 12k tokens of prompt, first content 4s later: ~3,000 tok/s of apparent prefill. The
    # figure an operator watches across the turns of one thread to see a cache miss.
    assert LastRequestUsage(prefill_tokens=12_000, prefill_ms=4_000).prefill_tokens_per_second == (
        3000.0
    )


def test_the_prefill_rate_is_null_whenever_a_term_is_missing():
    """Absent, never a guess. A response that emitted no content at all has no time to first
    token; a thread whose prompt size could not be estimated has no numerator. Either way the
    honest answer is nothing — this figure is read as a cache diagnosis, and a fabricated one
    would be read as a diagnosis too."""
    assert LastRequestUsage(prefill_ms=4_000).prefill_tokens_per_second is None
    assert LastRequestUsage(prefill_tokens=12_000).prefill_tokens_per_second is None
    assert LastRequestUsage(prefill_tokens=12_000, prefill_ms=0).prefill_tokens_per_second is None


def test_the_prefill_rate_is_not_taken_from_the_reported_prompt_tokens():
    """The reason there is a second numerator at all. A local server routinely reports
    `input_tokens=0` meaning "not measured", so a rate over `input_tokens` would be absent on
    exactly the endpoints this figure exists for. `prefill_tokens` carries the
    measured-or-estimated prompt size instead, and the two are independent by design."""
    usage = LastRequestUsage(input_tokens=0, prefill_tokens=8_000, prefill_ms=2_000)
    assert usage.prefill_tokens_per_second == 4000.0


def _measured_turn(*, ttft_ms: int | None) -> RunMetrics:
    """One response's worth of turn, with the stopwatch reporting ``ttft_ms``."""
    run = Run(id="r", kind="chat", owner_id="operator", stream=RunStream())
    run.timer.responses.append(ResponseTiming(llm_ms=5_000, ttft_ms=ttft_ms))
    messages = [_prompt("hi"), _response(input_tokens=1_000, output_tokens=120)]
    return turn_metrics(run, messages, settings=Settings())


def test_the_turn_attaches_the_prompt_size_and_the_wait_it_measured():
    """The two figures the rate is over are not in the message history — the timings are on
    the run's own stopwatch and the prompt size is the footprint the frame just computed — so
    the metrics builder is where they are joined."""
    metrics = _measured_turn(ttft_ms=2_000)

    assert metrics.last_request is not None
    assert metrics.last_request.prefill_ms == 2_000
    # The numerator is the footprint **less this response's own output**: the footprint is
    # prompt plus generation, and only the prompt is prefilled.
    assert metrics.context_used is not None
    assert metrics.last_request.prefill_tokens == metrics.context_used - 120
    assert metrics.last_request.prefill_tokens_per_second is not None


def test_a_response_that_produced_no_first_token_reports_no_prefill_figures():
    """A bare tool call the timer saw no content from. The route and the cache figures still
    stand — they are properties of the request — but there is no wait to divide by."""
    metrics = _measured_turn(ttft_ms=None)

    assert metrics.last_request is not None
    assert metrics.last_request.prefill_ms is None
    assert metrics.last_request.prefill_tokens is None
    assert metrics.last_request.prefill_tokens_per_second is None
