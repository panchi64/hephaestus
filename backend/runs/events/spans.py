"""``span.completed``: one finished agent run, model request or tool call, as traced."""

from datetime import datetime
from typing import Literal

from .base import Body

SpanKind = Literal["run", "model", "tool"]


class SpanCompleted(Body):
    """Timing, tokens and cost of one traced step; never its content.

    ``parent_span_id`` nests model and tool spans under their agent run, and a sub-agent's
    run under the tool call that started it. Token and cost fields are null when the
    provider reported nothing or the model has no published price."""

    type: Literal["span.completed"] = "span.completed"
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
