"""Agent tracing: span collection, attribution, the trace store, and its routes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.trace import SpanContext
from pydantic_ai import Agent, InstrumentationSettings, ModelResponse, TextPart
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RequestUsage
from sqlalchemy import Engine

from core.db import init_db, make_engine
from observability import SpanCollector, SpanRecord, to_record
from observability.tracing import collector, install_tracing, tracing_active
from routes.deps import app_state
from runs.events import SpanKind
from runs.run import Run
from runs.scope import current_run
from runs.stream import RunStream
from services.conversations.usage import response_cost
from services.traces import TraceStore

from ._helpers import client_app
from ._json import json_list, obj


def _traced_agent(collected: list[tuple[SpanRecord, Run | None]], name: str) -> Agent[None, str]:
    collector = SpanCollector()
    collector.retarget(lambda record, run: collected.append((record, run)))
    provider = TracerProvider()
    provider.add_span_processor(collector)
    agent = Agent(TestModel(), name=name)
    agent.instrument = InstrumentationSettings(tracer_provider=provider, include_content=False)

    @agent.tool_plain
    def lookup(city: str) -> str:
        return f"sunny in {city}"

    return agent


def _run(conversation_id: str | None = "conv-1") -> Run:
    return Run(
        id="run-1",
        kind="interactive",
        owner_id="operator",
        stream=RunStream(),
        conversation_id=conversation_id,
    )


async def test_a_run_yields_run_model_and_tool_spans_attributed_to_the_current_run():
    collected: list[tuple[SpanRecord, Run | None]] = []
    agent = _traced_agent(collected, "main")
    run = _run()
    token = current_run.set(run)
    try:
        _ = await agent.run("weather?")
    finally:
        current_run.reset(token)

    records = [r for r, _ in collected]
    kinds = [r.kind for r in records]
    assert kinds.count("run") == 1
    assert kinds.count("model") == 2
    assert kinds.count("tool") == 1
    assert {r.agent_name for r in records} == {"main"}
    assert {r.run_id for r in records} == {"run-1"}
    assert {r.conversation_id for r in records} == {"conv-1"}
    assert all(r is run for _, r in collected)

    (tool,) = [r for r in records if r.kind == "tool"]
    assert tool.tool_name == "lookup"
    root = next(r for r in records if r.kind == "run")
    assert tool.parent_span_id == root.span_id
    model = next(r for r in records if r.kind == "model")
    # Background cost is attributed through this link (`TraceStore.background`).
    assert model.parent_span_id == root.span_id
    assert model.model == "test"
    assert model.input_tokens is not None and model.output_tokens is not None


async def test_work_outside_any_run_is_unattributed():
    collected: list[tuple[SpanRecord, Run | None]] = []
    agent = _traced_agent(collected, "mail_triage")
    _ = await agent.run("triage this")
    assert collected
    assert all(run is None for _, run in collected)
    assert {r.conversation_id for r, _ in collected} == {None}


def _engine() -> Engine:
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    return engine


def _record(
    span_id: str,
    *,
    kind: SpanKind = "model",
    agent: str = "main",
    run_id: str | None = "run-1",
    conversation_id: str | None = "conv-1",
    trace_id: str = "t1",
    parent: str | None = None,
    cost: float | None = 0.01,
    started_at: datetime | None = None,
    duration_ms: int = 100,
) -> SpanRecord:
    return SpanRecord(
        span_id=span_id,
        trace_id=trace_id,
        parent_span_id=parent,
        kind=kind,
        agent_name=agent,
        model="claude-haiku-5-5",
        tool_name="lookup" if kind == "tool" else None,
        started_at=started_at or datetime.now(UTC),
        duration_ms=duration_ms,
        ttft_ms=None,
        input_tokens=10 if kind == "model" else None,
        output_tokens=5 if kind == "model" else None,
        cache_read_tokens=None,
        cache_write_tokens=None,
        cost_usd=cost if kind == "model" else None,
        error_type=None,
        run_id=run_id,
        conversation_id=conversation_id,
        owner_id="operator",
    )


async def test_turns_roll_up_per_run_and_split_out_background_cost():
    store = TraceStore(_engine(), owner_id="operator")
    start = datetime.now(UTC)
    store.add(_record("a", kind="run", cost=None, started_at=start, duration_ms=900))
    store.add(_record("b", cost=0.02, started_at=start + timedelta(milliseconds=10)))
    store.add(_record("c", kind="tool", started_at=start + timedelta(milliseconds=200)))
    store.add(_record("d", agent="title", trace_id="t2", cost=0.001, started_at=start))

    (turn,) = await store.conversation_turns("conv-1")
    assert turn.run_id == "run-1"
    assert turn.model_calls == 2
    assert turn.tool_calls == 1
    assert turn.duration_ms == 900
    assert turn.cost_usd == 0.021
    assert turn.chat_cost_usd == 0.02


async def test_background_groups_every_non_chat_agent_by_name_and_day():
    store = TraceStore(_engine(), owner_id="operator")
    store.add(_record("r1", kind="run", agent="mail_triage", run_id=None, conversation_id=None))
    store.add(
        _record(
            "m1", agent="mail_triage", parent="r1", cost=0.003, run_id=None, conversation_id=None
        )
    )
    store.add(_record("r2", kind="run", agent="title", trace_id="t2"))
    store.add(_record("m2", agent="title", trace_id="t2", parent="r2", cost=0.001))
    store.add(_record("m3", agent="main", trace_id="t3", cost=0.5))

    activity = await store.background(7)
    by_name = {a.agent_name: a for a in activity.agents}
    assert set(by_name) == {"mail_triage", "title"}
    assert by_name["mail_triage"].calls == 1
    assert by_name["mail_triage"].cost_usd == 0.003
    assert len(activity.per_day) == 7
    assert activity.per_day[-1].day == datetime.now(UTC).date()
    assert sum(d.cost_usd for d in activity.per_day) == 0.004
    assert {c.agent_name for c in activity.recent} == {"mail_triage", "title"}


async def test_background_agents_sharing_a_turn_trace_are_each_charged_their_own_requests():
    store = TraceStore(_engine(), owner_id="operator")
    for run, model, cost in (("d1", "dm1", 0.002), ("d2", "dm2", 0.005)):
        store.add(_record(run, kind="run", agent="page_distill"))
        store.add(_record(model, agent="page_distill", parent=run, cost=cost))
    store.add(_record("main-m", agent="main", cost=0.5))

    activity = await store.background(7)
    (distill,) = activity.agents
    assert distill.cost_usd == 0.007
    assert sorted(c.cost_usd or 0.0 for c in activity.recent) == [0.002, 0.005]
    assert sum(d.cost_usd for d in activity.per_day) == 0.007


async def test_deleting_a_thread_drops_its_spans_saved_and_pending():
    store = TraceStore(_engine(), owner_id="operator")
    store.add(_record("saved"))
    store.add(_record("other", run_id="run-2", conversation_id="conv-2"))
    await store.flush()
    store.add(_record("pending"))

    await store.delete_for_conversations("operator", {"conv-1"})

    assert await store.run_trace("run-1") == []
    assert [r.span_id for r in await store.run_trace("run-2")] == ["other"]


async def test_prune_drops_spans_past_the_retention_window():
    store = TraceStore(_engine(), owner_id="operator")
    now = datetime.now(UTC)
    store.add(_record("old", started_at=now - timedelta(days=31)))
    store.add(_record("new", started_at=now - timedelta(days=1)))
    await store.flush()
    await store.prune(now=now)
    rows = await store.run_trace("run-1")
    assert [r.span_id for r in rows] == ["new"]


async def test_trace_routes_serve_saved_spans():
    async with client_app() as (client, app):
        traces = app_state(app).traces
        traces.add(_record("a", kind="run", cost=None))
        traces.add(_record("b"))

        turns = await client.get("/conversations/conv-1/traces")
        assert turns.status_code == 200
        assert obj(json_list(turns)[0])["run_id"] == "run-1"

        spans = await client.get("/runs/run-1/trace")
        assert spans.status_code == 200
        assert [obj(s)["kind"] for s in json_list(spans)] == ["run", "model"]

        missing = await client.get("/runs/nope/trace")
        assert missing.status_code == 404

        background = await client.get("/traces/background?days=7")
        assert background.status_code == 200


async def test_install_tracing_is_idempotent_and_undo_only_detaches_its_own_sink():
    async with client_app() as (_client, app):
        sink = app_state(app).span_sink
        assert collector.target_is(sink.deliver)
        undo_again = install_tracing(sink)
        assert tracing_active()
        undo_again()
        assert not collector.target_is(sink.deliver)


_HAIKU = "claude-3-5-haiku-20241022"


_CACHED_READ = RequestUsage(input_tokens=12_000, output_tokens=800, cache_read_tokens=9_000)


def _chat_span(address: str, usage: RequestUsage = _CACHED_READ) -> ReadableSpan:
    """A model span carrying ``usage`` the way pydantic-ai writes it onto one."""
    start = 1_780_000_000_000_000_000
    return ReadableSpan(
        name=f"chat {_HAIKU}",
        context=SpanContext(trace_id=1, span_id=2, is_remote=False),
        attributes={
            "gen_ai.operation.name": "chat",
            "gen_ai.agent.name": "main",
            "gen_ai.response.model": _HAIKU,
            "server.address": address,
            **usage.opentelemetry_attributes(),
        },
        start_time=start,
        end_time=start + 2_000_000_000,
    )


@pytest.mark.parametrize(
    "usage",
    [
        _CACHED_READ,
        # Anthropic's one-hour writes reach the span only as a usage detail.
        RequestUsage(
            input_tokens=12_000,
            output_tokens=800,
            cache_write_tokens=6_000,
            cache_write_1h_tokens=6_000,
            details={"ephemeral_1h_input_tokens": 6_000},
        ),
    ],
    ids=["cache-read", "one-hour-cache-write"],
)
def test_a_model_span_is_priced_exactly_as_the_conversation_totals_price_it(usage: RequestUsage):
    record = to_record(_chat_span("api.anthropic.com", usage), None)
    assert record is not None
    response = ModelResponse(
        parts=[TextPart("ok")],
        usage=usage,
        model_name=_HAIKU,
        provider_url="https://api.anthropic.com",
        timestamp=record.started_at,
    )
    expected = response_cost(response)
    assert expected is not None
    assert record.cost_usd == expected.total_usd


def test_a_local_server_serving_a_hosted_model_name_is_not_priced():
    record = to_record(_chat_span("localhost"), None)
    assert record is not None
    assert record.cost_usd is None
