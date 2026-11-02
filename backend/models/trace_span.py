"""Traced agent work: one row per finished agent run, model request or tool call.

Metadata only — timing, tokens, cost, names. Prompts, arguments and results never reach a
span (`observability/tracing.py` turns content capture off), so nothing here is sealed.
Rows older than the retention window are pruned by `services/traces.py`.
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from sqlmodel import Field

from models._table import Table


class TraceSpan(Table, table=True):
    __tablename__: ClassVar[str] = "trace_spans"

    span_id: str = Field(primary_key=True)
    owner_id: str = Field(index=True)
    trace_id: str = Field(index=True)
    parent_span_id: str | None = None
    # Null for work no run started: mail triage, the work-summary sweep, a title asked
    # for from the thread list.
    run_id: str | None = Field(default=None, index=True)
    conversation_id: str | None = Field(default=None, index=True)
    # "run", "model" or "tool".
    kind: str
    agent_name: str = Field(index=True)
    model: str | None = None
    tool_name: str | None = None
    started_at: datetime = Field(index=True)
    duration_ms: int
    ttft_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    cost_usd: float | None = None
    error_type: str | None = None
