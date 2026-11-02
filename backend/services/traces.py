"""Saved spans: a write-behind buffer, the trace views' queries, and 30-day retention."""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import date, datetime, timedelta

from pydantic import BaseModel
from sqlalchemy import Engine
from sqlmodel import Session, col, delete, select
from sqlmodel.sql.expression import SelectOfScalar

from core.db import in_session
from core.periodic import PeriodicTask
from core.serde import as_utc
from models._fields import utcnow
from models.trace_span import TraceSpan
from observability import SpanRecord
from observability.names import MAIN_AGENT, TURN_AGENTS

logger = logging.getLogger(__name__)

RETENTION = timedelta(days=30)
_FLUSH_INTERVAL_S = 1.0
_PRUNE_INTERVAL_S = 24 * 3600.0
_RECENT_LIMIT = 50


class TurnSummary(BaseModel):
    """One run of a conversation, rolled up from its spans."""

    run_id: str
    started_at: datetime
    duration_ms: int
    model_calls: int
    tool_calls: int
    input_tokens: int | None
    output_tokens: int | None
    # Everything the run paid for, and the part the chat agent itself spent; the
    # difference is background work the turn started (its title, a page distill).
    cost_usd: float | None
    chat_cost_usd: float | None
    errors: int


class AgentActivity(BaseModel):
    agent_name: str
    calls: int
    cost_usd: float | None
    avg_duration_ms: int
    last_run_at: datetime


class DayCost(BaseModel):
    day: date
    cost_usd: float


class BackgroundCall(BaseModel):
    span_id: str
    agent_name: str
    model: str | None
    conversation_id: str | None
    started_at: datetime
    duration_ms: int
    cost_usd: float | None
    error_type: str | None


class BackgroundActivity(BaseModel):
    """Work done by every agent other than the chat agent and its sub-agents."""

    days: int
    agents: list[AgentActivity]
    per_day: list[DayCost]
    recent: list[BackgroundCall]


class TraceStore:
    def __init__(self, engine: Engine, *, owner_id: str) -> None:
        self._engine: Engine = engine
        self._owner_id: str = owner_id
        self._pending: list[TraceSpan] = []
        self._flusher: PeriodicTask = PeriodicTask(
            "trace-flush", interval_s=_FLUSH_INTERVAL_S, work=self.flush, logger=logger
        )
        self._pruner: PeriodicTask = PeriodicTask(
            "trace-prune",
            interval_s=_PRUNE_INTERVAL_S,
            first_delay_s=60.0,
            work=self.prune,
            logger=logger,
            log_level=logging.WARNING,
        )

    async def start(self) -> None:
        await self._flusher.start()
        await self._pruner.start()

    async def stop(self) -> None:
        await self._pruner.stop()
        await self._flusher.stop()
        await self.flush()

    def add(self, record: SpanRecord) -> None:
        self._pending.append(
            TraceSpan(
                span_id=record.span_id,
                owner_id=record.owner_id or self._owner_id,
                trace_id=record.trace_id,
                parent_span_id=record.parent_span_id,
                run_id=record.run_id,
                conversation_id=record.conversation_id,
                kind=record.kind,
                agent_name=record.agent_name,
                model=record.model,
                tool_name=record.tool_name,
                started_at=record.started_at,
                duration_ms=record.duration_ms,
                ttft_ms=record.ttft_ms,
                input_tokens=record.input_tokens,
                output_tokens=record.output_tokens,
                cache_read_tokens=record.cache_read_tokens,
                cache_write_tokens=record.cache_write_tokens,
                cost_usd=record.cost_usd,
                error_type=record.error_type,
            )
        )

    async def flush(self) -> None:
        batch, self._pending = self._pending, []
        if not batch:
            return

        def write(session: Session) -> None:
            for row in batch:
                _ = session.merge(row)

        await in_session(self._engine, write)

    async def prune(self, *, now: datetime | None = None) -> None:
        cutoff = (now or utcnow()) - RETENTION

        def drop(session: Session) -> None:
            _ = session.exec(delete(TraceSpan).where(col(TraceSpan.started_at) < cutoff))

        await in_session(self._engine, drop)

    async def run_trace(self, run_id: str) -> list[TraceSpan]:
        await self.flush()
        return await self._select(
            select(TraceSpan)
            .where(TraceSpan.owner_id == self._owner_id, TraceSpan.run_id == run_id)
            .order_by(col(TraceSpan.started_at))
        )

    async def conversation_turns(self, conversation_id: str) -> list[TurnSummary]:
        await self.flush()
        spans = await self._select(
            select(TraceSpan).where(
                TraceSpan.owner_id == self._owner_id,
                TraceSpan.conversation_id == conversation_id,
                col(TraceSpan.run_id).is_not(None),
            )
        )
        by_run: dict[str, list[TraceSpan]] = defaultdict(list)
        for span in spans:
            if span.run_id is not None:
                by_run[span.run_id].append(span)
        turns = [_summarize_turn(run_id, rows) for run_id, rows in by_run.items()]
        return sorted(turns, key=lambda t: t.started_at)

    async def background(self, days: int, *, now: datetime | None = None) -> BackgroundActivity:
        await self.flush()
        since = (now or utcnow()) - timedelta(days=days)
        spans = await self._select(
            select(TraceSpan).where(
                TraceSpan.owner_id == self._owner_id,
                col(TraceSpan.agent_name).not_in(TURN_AGENTS),
                col(TraceSpan.started_at) >= since,
            )
        )
        cost_by_trace: dict[str, list[float | None]] = defaultdict(list)
        for span in spans:
            if span.kind == "model":
                cost_by_trace[span.trace_id].append(span.cost_usd)
        runs = sorted(
            (s for s in spans if s.kind == "run"), key=lambda s: s.started_at, reverse=True
        )

        by_agent: dict[str, list[TraceSpan]] = defaultdict(list)
        per_day: dict[date, float] = defaultdict(float)
        for run in runs:
            by_agent[run.agent_name].append(run)
            cost = _sum(cost_by_trace[run.trace_id])
            if cost is not None:
                per_day[run.started_at.date()] += cost
        agents = [
            AgentActivity(
                agent_name=name,
                calls=len(rows),
                cost_usd=_sum(c for r in rows for c in cost_by_trace[r.trace_id]),
                avg_duration_ms=sum(r.duration_ms for r in rows) // len(rows),
                last_run_at=rows[0].started_at,
            )
            for name, rows in by_agent.items()
        ]
        recent = [
            BackgroundCall(
                span_id=run.span_id,
                agent_name=run.agent_name,
                model=run.model,
                conversation_id=run.conversation_id,
                started_at=run.started_at,
                duration_ms=run.duration_ms,
                cost_usd=_sum(cost_by_trace[run.trace_id]),
                error_type=run.error_type,
            )
            for run in runs[:_RECENT_LIMIT]
        ]
        return BackgroundActivity(
            days=days,
            agents=sorted(agents, key=lambda a: a.cost_usd or 0.0, reverse=True),
            per_day=[DayCost(day=d, cost_usd=c) for d, c in sorted(per_day.items())],
            recent=recent,
        )

    async def _select(self, statement: SelectOfScalar[TraceSpan]) -> list[TraceSpan]:
        rows = await in_session(self._engine, lambda s: list(s.exec(statement)))
        for row in rows:
            row.started_at = as_utc(row.started_at) or row.started_at
        return rows


def _summarize_turn(run_id: str, rows: Sequence[TraceSpan]) -> TurnSummary:
    models = [r for r in rows if r.kind == "model"]
    start = min(r.started_at for r in rows)
    end = max(r.started_at + timedelta(milliseconds=r.duration_ms) for r in rows)
    return TurnSummary(
        run_id=run_id,
        started_at=start,
        duration_ms=int((end - start).total_seconds() * 1000),
        model_calls=len(models),
        tool_calls=sum(1 for r in rows if r.kind == "tool"),
        input_tokens=_sum_int(r.input_tokens for r in models),
        output_tokens=_sum_int(r.output_tokens for r in models),
        cost_usd=_sum(r.cost_usd for r in models),
        chat_cost_usd=_sum(r.cost_usd for r in models if r.agent_name == MAIN_AGENT),
        errors=sum(1 for r in rows if r.error_type is not None),
    )


def _sum(values: Iterable[float | None]) -> float | None:
    """None when nothing was priced, so an unpriced model reads as absent rather than free."""
    present = [v for v in values if v is not None]
    return sum(present) if present else None


def _sum_int(values: Iterable[int | None]) -> int | None:
    present = [v for v in values if v is not None]
    return sum(present) if present else None
