"""Turns pydantic-ai's finished OpenTelemetry spans into :class:`SpanRecord` values."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import override

from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor
from opentelemetry.trace import StatusCode
from opentelemetry.util.types import AttributeValue
from pydantic_ai.usage import RequestUsage

from runs.events import SpanCompleted, SpanKind
from runs.run import Run
from runs.scope import current_run
from services.pricing import price_usage

_KINDS: dict[str, SpanKind] = {"invoke_agent": "run", "chat": "model", "execute_tool": "tool"}
_DETAILS = "gen_ai.usage.details."


@dataclass(frozen=True, slots=True)
class SpanRecord:
    """One finished step, attributed to the run that was current when it started."""

    span_id: str
    trace_id: str
    parent_span_id: str | None
    kind: SpanKind
    agent_name: str
    model: str | None
    tool_name: str | None
    started_at: datetime
    duration_ms: int
    ttft_ms: int | None
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    cost_usd: float | None
    error_type: str | None
    run_id: str | None
    conversation_id: str | None
    owner_id: str | None

    def event(self) -> SpanCompleted:
        return SpanCompleted(
            span_id=self.span_id,
            trace_id=self.trace_id,
            parent_span_id=self.parent_span_id,
            kind=self.kind,
            agent_name=self.agent_name,
            model=self.model,
            tool_name=self.tool_name,
            started_at=self.started_at,
            duration_ms=self.duration_ms,
            ttft_ms=self.ttft_ms,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cache_read_tokens=self.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens,
            cost_usd=self.cost_usd,
            error_type=self.error_type,
        )


type Deliver = Callable[[SpanRecord, Run | None], None]


class SpanCollector(SpanProcessor):
    """Keeps agent-run, model-request and tool spans and hands each to ``deliver``.

    ``deliver`` is called from whichever thread ended the span; the receiver owns getting
    back onto the event loop."""

    def __init__(self) -> None:
        self._deliver: Deliver | None = None
        # The run current at span start, keyed by span id: `on_end` may run outside the
        # task that opened the span, where the contextvar no longer holds it.
        self._runs: dict[int, Run] = {}

    def retarget(self, deliver: Deliver | None) -> None:
        self._deliver = deliver

    def target_is(self, deliver: Deliver) -> bool:
        return self._deliver == deliver

    @override
    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        run = current_run.get()
        if run is not None and span.context is not None:
            self._runs[span.context.span_id] = run

    @override
    def on_end(self, span: ReadableSpan) -> None:
        context = span.context
        run = self._runs.pop(context.span_id, None) if context is not None else None
        deliver = self._deliver
        if deliver is None:
            return
        record = to_record(span, run)
        if record is not None:
            deliver(record, run)

    @override
    def shutdown(self) -> None:
        self._runs.clear()

    @override
    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


def to_record(span: ReadableSpan, run: Run | None) -> SpanRecord | None:
    """The record for a span pydantic-ai emitted, or None for any other span."""
    attrs: Mapping[str, AttributeValue] = span.attributes or {}
    kind = _KINDS.get(_str(attrs.get("gen_ai.operation.name")) or "")
    context = span.context
    if kind is None or context is None or span.start_time is None or span.end_time is None:
        return None
    # Run spans carry the run's cumulative usage under `aggregated_usage`, so summing
    # model spans and run spans never double-counts.
    usage = "gen_ai.aggregated_usage." if kind == "run" else "gen_ai.usage."
    ttft_s = attrs.get("gen_ai.client.operation.time_to_first_chunk")
    model = (
        _str(attrs.get("gen_ai.response.model"))
        or _str(attrs.get("gen_ai.request.model"))
        or _str(attrs.get("model_name"))
    )
    started_at = datetime.fromtimestamp(span.start_time / 1e9, UTC)
    input_tokens = _int(attrs.get(f"{usage}input_tokens"))
    output_tokens = _int(attrs.get(f"{usage}output_tokens"))
    cache_read = _int(attrs.get(f"{usage}cache_read.input_tokens"))
    cache_write = _int(attrs.get(f"{usage}cache_creation.input_tokens"))
    cost = None
    if kind == "model":
        # Priced here rather than read off `operation.cost`, which pydantic-ai falls back
        # to matching by provider name; see `price_usage` for why that is wrong here.
        address = _str(attrs.get("server.address"))
        priced = price_usage(
            _request_usage(attrs, input_tokens, output_tokens, cache_read, cache_write),
            model,
            f"https://{address}" if address else None,
            started_at,
        )
        cost = priced.total_usd if priced else None
    return SpanRecord(
        span_id=format(context.span_id, "016x"),
        trace_id=format(context.trace_id, "032x"),
        parent_span_id=format(span.parent.span_id, "016x") if span.parent else None,
        kind=kind,
        agent_name=_str(attrs.get("gen_ai.agent.name")) or "agent",
        model=model,
        tool_name=_str(attrs.get("gen_ai.tool.name")),
        started_at=started_at,
        duration_ms=(span.end_time - span.start_time) // 1_000_000,
        ttft_ms=round(ttft_s * 1000) if isinstance(ttft_s, int | float) else None,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        cost_usd=cost,
        error_type=_error_type(span),
        run_id=run.id if run else None,
        conversation_id=run.conversation_id if run else None,
        owner_id=run.owner_id if run else None,
    )


def _request_usage(
    attrs: Mapping[str, AttributeValue],
    input_tokens: int | None,
    output_tokens: int | None,
    cache_read: int | None,
    cache_write: int | None,
) -> RequestUsage:
    """The request's usage as `response_cost` sees it. The fields a span carries only
    under `gen_ai.usage.details.*` change the price too: Anthropic bills one-hour cache
    writes above five-minute ones, and audio tokens at their own rates."""

    def detail(*keys: str) -> int:
        return sum(_int(attrs.get(f"{_DETAILS}{key}")) or 0 for key in keys)

    return RequestUsage(
        input_tokens=input_tokens or 0,
        output_tokens=output_tokens or 0,
        cache_read_tokens=cache_read or 0,
        cache_write_tokens=cache_write or 0,
        # Compaction writes use the request's TTL; pydantic-ai sums them back the same way.
        cache_write_1h_tokens=detail(
            "ephemeral_1h_input_tokens", "compaction_ephemeral_1h_input_tokens"
        ),
        input_audio_tokens=detail("input_audio_tokens"),
        cache_audio_read_tokens=detail("cache_audio_read_tokens"),
        output_audio_tokens=detail("output_audio_tokens"),
    )


def _error_type(span: ReadableSpan) -> str | None:
    if span.status.status_code is not StatusCode.ERROR:
        return None
    for event in reversed(span.events):
        exc_type = _str((event.attributes or {}).get("exception.type"))
        if exc_type:
            return exc_type.rsplit(".", 1)[-1]
    return "Error"


def _str(value: AttributeValue | None) -> str | None:
    return value if isinstance(value, str) and value else None


def _int(value: AttributeValue | None) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
