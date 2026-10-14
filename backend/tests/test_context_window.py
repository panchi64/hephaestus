"""The context-window fullness derivation — the single owner of the fraction and
severity thresholds (events.ContextWindow), shared by live run metrics and the
on-load conversation detail."""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from pydantic_ai import (
    FunctionToolset,
    ModelRequest,
    ModelResponse,
)
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import TextPart, UserPromptPart
from pydantic_ai.models.test import TestModel
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.usage import RequestUsage

from agent import build_chat_orchestrator
from agent.model_errors import context_limit_message, is_context_overflow
from runs import ContextWindow, FoldPoint, Run, RunRegistry, RunStatus, RunStream
from runs.events import RunMetrics
from services.conversations import context_footprint


def _response(input_tokens: int, output_tokens: int) -> ModelResponse:
    return ModelResponse(
        parts=[TextPart(content="x")],
        usage=RequestUsage(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def test_footprint_is_last_response_not_a_sum():
    # A tool-calling / multi-turn run holds several responses; each later request
    # re-sends the growing history, so its input count already subsumes the prior.
    # The footprint must be the LAST response's prompt+generation, never the sum —
    # summing would overstate fullness several-fold (the bug this guards).
    messages = [
        ModelRequest(parts=[UserPromptPart(content="hi")]),
        _response(50, 5),
        ModelRequest(parts=[UserPromptPart(content="tool result")]),
        _response(140, 12),
    ]
    assert context_footprint(messages) == 152  # last response only, not 50+5+140+12


def test_footprint_none_without_response():
    assert context_footprint([ModelRequest(parts=[UserPromptPart(content="hi")])]) is None


def test_footprint_none_when_usage_unreported():
    # Local servers often report 0 input — treated as unmeasured, not a real 0.
    assert context_footprint([_response(0, 0)]) is None


def test_from_used_computes_fraction():
    state = ContextWindow.from_used(160_000, 200_000)
    assert state is not None
    assert state.used == 160_000
    assert state.window == 200_000
    assert state.fraction == pytest.approx(0.8)


@pytest.mark.parametrize(
    ("used", "expected"),
    [
        (100_000, "nominal"),
        (149_999, "nominal"),
        (150_000, "warn"),
        (179_999, "warn"),
        (180_000, "alert"),
    ],
)
def test_level_thresholds(used: int, expected: str):
    # 75% (150k) flips nominal→warn; 90% (180k) flips warn→alert, against a 200k window.
    state = ContextWindow.from_used(used, 200_000)
    assert state is not None
    assert state.level == expected


def test_fraction_clamps_when_over_full():
    state = ContextWindow.from_used(250_000, 200_000)
    assert state is not None
    assert state.fraction == 1.0
    assert state.level == "alert"


def test_none_without_window():
    assert ContextWindow.from_used(1_000, None) is None
    assert ContextWindow.from_used(1_000, 0) is None


def test_none_without_footprint():
    assert ContextWindow.from_used(None, 200_000) is None


def test_run_metrics_exposes_context_on_the_wire():
    # context_used is the last-response footprint, deliberately distinct from the
    # cumulative input/output token totals on the same frame.
    metrics = RunMetrics(
        input_tokens=999_999, output_tokens=999_999, context_used=195_000, context_window=200_000
    )
    payload = metrics.model_dump(mode="json")
    assert payload["context"] == {
        "used": 195_000,
        "window": 200_000,
        "fraction": pytest.approx(0.975),
        "level": "alert",
        # Null, not absent: this frame was built without a measured composition (no turn
        # assembled a request), and the split is one of the things that is absent rather
        # than zeroed when it wasn't measured.
        "parts": None,
        # Same rule: this frame carried no compaction policy, which is not the same
        # statement as "folding is off" — that one arrives as `active: false` with the
        # fraction still on it, so the gauge can dim the mark instead of dropping it.
        "fold": None,
    }


def test_the_fold_point_rides_on_the_window_it_is_a_fraction_of():
    """The mark and the fullness are one reading, so they travel together — and the
    fold never touches `level`, which stays the operator's warn/alert story alone."""
    metrics = RunMetrics(
        context_used=100_000,
        context_window=200_000,
        context_fold=FoldPoint(fraction=0.8, active=True),
    )
    payload = metrics.model_dump(mode="json")
    assert payload["context"]["fold"] == {"fraction": 0.8, "active": True}
    # Half full against a 75/90 pair: a fold point at 0.8 must not have moved severity.
    assert payload["context"]["level"] == "nominal"
    # The seed itself is an input to the derivation, not a second copy on the frame.
    assert "context_fold" not in payload


@pytest.mark.parametrize("fraction", [0.0, -0.5, 1.5])
def test_a_nonsense_threshold_draws_no_mark_rather_than_raising(fraction: float):
    """A readout must not be able to stop the thing it describes.

    ``Settings.auto_compact_threshold`` is an unbounded float — ``_float_or`` bounds what
    the settings store holds and then falls back to it — so a deploy running with
    ``HEPHAESTUS_AUTO_COMPACT_THRESHOLD=0`` reaches here. Constructed directly, the field
    bounds would raise inside the orchestrator (killing every turn) and 500 every
    conversation load, over a mark on a bar."""
    assert FoldPoint.of(fraction, active=True) is None


def test_a_valid_threshold_still_draws():
    mark = FoldPoint.of(0.8, active=False)
    assert mark is not None
    assert (mark.fraction, mark.active) == (0.8, False)


def test_a_thread_with_folding_off_still_says_where_the_fold_would_be():
    metrics = RunMetrics(
        context_used=100_000,
        context_window=200_000,
        context_fold=FoldPoint(fraction=0.8, active=False),
    )
    fold = metrics.model_dump(mode="json")["context"]["fold"]
    assert fold == {"fraction": 0.8, "active": False}


def test_run_metrics_context_null_without_window():
    payload = RunMetrics(context_used=100, output_tokens=5).model_dump(mode="json")
    assert payload["context"] is None


def test_run_metrics_context_null_without_footprint():
    # Cumulative totals present but no measured footprint → still null.
    payload = RunMetrics(input_tokens=500, output_tokens=20, context_window=200_000).model_dump(
        mode="json"
    )
    assert payload["context"] is None


# --- the context-window stop: overflow halts cleanly, naming the limit -------


_DEFAULT_CTX_MSG = "This model's maximum context length is 8192 tokens."


def _ctx_error(message: str = _DEFAULT_CTX_MSG) -> ModelHTTPError:
    return ModelHTTPError(status_code=400, model_name="m", body={"message": message})


@pytest.mark.parametrize(
    "message",
    [
        "This model's maximum context length is 8192 tokens.",
        "prompt is too long: 205000 tokens > 200000 maximum",
        "Error code: 400 - context_length_exceeded",
        "the request exceeds the context window (n_ctx=4096)",
    ],
)
def test_is_context_overflow_detects_provider_phrasings(message: str):
    assert is_context_overflow(_ctx_error(message))


@pytest.mark.parametrize(
    "message",
    [
        "internal server error",
        # Generic phrasings deliberately NOT treated as overflow — they also appear in
        # rate-limit/validation errors, and misclassifying one would block the run with a
        # misleading context-window stop while swallowing the real error.
        "rate limit reached: too many tokens per minute",
        "validation error: please reduce the length of field 'name'",
        "the model's context window is 200000 tokens",  # mentions it, not an overflow
    ],
)
def test_is_context_overflow_ignores_unrelated_errors(message: str):
    assert not is_context_overflow(_ctx_error(message))


def test_a_provider_error_code_decides_without_consulting_the_prose():
    # A published error code is the structured signal the text scan stands in for. Where
    # one exists, reading it is not a heuristic — so the message is not consulted at all.
    coded = ModelHTTPError(
        status_code=400,
        model_name="m",
        body={"error": {"code": "context_length_exceeded", "message": "nothing familiar here"}},
    )
    assert is_context_overflow(coded)

    # And the converse: a provider that named its error something else has answered the
    # question, even when its prose happens to carry one of our markers.
    other = ModelHTTPError(
        status_code=400,
        model_name="m",
        body={
            "error": {
                "code": "invalid_prompt",
                "message": "maximum context length is mentioned here but that isn't the fault",
            }
        },
    )
    assert not is_context_overflow(other)


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500, 503])
def test_the_prose_scan_never_runs_on_a_status_an_overflow_cannot_arrive_as(status: int):
    # The failure this guards: a rate-limit or server-fault body that quotes the prompt's
    # token counts used to be classified as a context ceiling, stopping the run with a
    # misleading message and swallowing the real, actionable error.
    exc = ModelHTTPError(
        status_code=status,
        model_name="m",
        body={"message": "This model's maximum context length is 8192 tokens."},
    )
    assert not is_context_overflow(exc)


@pytest.mark.parametrize("status", [400, 413, 422])
def test_the_prose_scan_still_runs_for_a_local_engine_that_sends_no_code(status: int):
    # llama.cpp/LM Studio/vLLM answer in English with no error object — the scan is the
    # only signal there is, and must keep working.
    exc = ModelHTTPError(
        status_code=status,
        model_name="m",
        body="the request exceeds the context window (n_ctx=4096)",
    )
    assert is_context_overflow(exc)


def test_context_limit_message_names_the_window():
    run = Run(id="t", kind="chat", owner_id="op", stream=RunStream())
    run.context_window = 128_000
    assert "128,000" in context_limit_message(run)


class _ContextOverflowModel(WrapperModel):
    """A model whose request overruns the context window, the way a provider rejects an
    over-long prompt — so a run drives straight into the overflow."""

    def __init__(self) -> None:
        super().__init__(TestModel())

    async def request(self, *args, **kwargs):
        raise _ctx_error()

    @asynccontextmanager
    async def request_stream(self, *args, **kwargs):
        raise _ctx_error()
        yield  # unreachable — keeps this a generator


async def test_context_overflow_stops_the_run_naming_the_limit():
    reg = RunRegistry()
    orch = build_chat_orchestrator("hi", model=_ContextOverflowModel(), context_window=8192)
    run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch)
    await run.wait()

    # Stopped (blocked), not errored, not silently degraded.
    assert run.status is RunStatus.blocked
    notice = next(e.body for e in run.stream.replay() if e.body.type == "limit.notice")
    assert notice.limit == "context"
    assert "8,192" in notice.message  # the operator sees the actual ceiling


# --- the live gauge: context/usage frames stream as the turn progresses ------


async def test_run_metrics_emitted_live_per_step_not_just_at_the_end():
    util = FunctionToolset()

    # Declares `read`: composed here rather than shipped, so the name registry has never
    # heard of `util_ping` and would gate it. This test is about the metrics frames, and a
    # gated tool would park the run before the second step it needs.
    @util.tool_plain(metadata={"sensitivity": "read"})
    def ping() -> str:  # namespaced to `util_ping`; TestModel calls it, forcing a 2nd step
        return "pong"

    reg = RunRegistry()
    # TestModel calls the available tool, then answers ⇒ two model requests (two steps).
    orch = build_chat_orchestrator("hi", model=TestModel(), categories={"util": util})
    run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch)
    await run.wait()

    assert run.status is RunStatus.done
    metrics = [e.body for e in run.stream.replay() if e.body.type == "run.metrics"]
    # Two model requests (the tool call + the answer) ⇒ live frames during the run, beyond
    # the single terminal frame the registry emits — so the gauge fills as the turn runs.
    assert len(metrics) >= 2
