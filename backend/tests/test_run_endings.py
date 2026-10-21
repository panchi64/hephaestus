"""Why a run stopped: the ending code, the turn cap and the model's Retry-After."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path

import pytest
from pydantic_ai import FunctionToolset
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelRequest, UserPromptPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel

import agent.engine as engine
import agent.turn as turn
from agent import build_chat_orchestrator
from agent.turn import TURN_LIMIT_DETAIL, transient_wait_s
from core.config import Settings
from core.worker import WriteBehindWorker
from harness.manifests.notifications import save_failure_reporter
from runs import LimitNotice, Run, RunEnded, RunError, RunRegistry, RunStatus
from runs.run import StopCause
from services.notifications import NotificationService
from tools import RunDeps

from ._agent_helpers import make_stores
from ._helpers import bodies_of


def _toolset() -> FunctionToolset[RunDeps]:
    toolset: FunctionToolset[RunDeps] = FunctionToolset()

    @toolset.tool_plain(metadata={"sensitivity": "read"})
    def step(n: int) -> str:
        return f"step {n} done"

    return toolset


def _calls_step() -> FunctionModel:
    """Calls ``work_step`` with the same arguments on every request."""

    async def stream_fn(
        _messages: list[ModelMessage], _info: AgentInfo
    ) -> AsyncIterator[str | DeltaToolCalls]:
        yield {0: DeltaToolCall(name="work_step", json_args=json.dumps({"n": 1}))}

    return FunctionModel(stream_function=stream_fn)


def _settings(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    monkeypatch.setattr(engine, "get_settings", lambda: settings)


def _ended(run: Run) -> RunEnded:
    body = run.stream.replay()[-1].body
    assert isinstance(body, RunEnded)
    return body


# --- ending codes -----------------------------------------------------------


async def test_wall_clock_bound_sets_its_code_and_stop_cause() -> None:
    reg = RunRegistry(wall_clock_timeout_s=0.05, inactivity_timeout_s=None)

    async def orch(_run: Run) -> None:
        await asyncio.sleep(5)

    run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch)
    await run.wait()

    assert run.status is RunStatus.blocked
    assert _ended(run).code == "wall_clock"
    assert run.stop_cause is StopCause.time_limit


async def test_inactivity_bound_sets_its_code() -> None:
    reg = RunRegistry(wall_clock_timeout_s=5.0, inactivity_timeout_s=0.05)

    async def orch(_run: Run) -> None:
        await asyncio.sleep(5)

    run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch)
    await run.wait()

    assert _ended(run).code == "inactivity"
    assert run.stop_cause is None


async def test_a_completed_run_has_no_code() -> None:
    async def orch(_run: Run) -> None:
        return None

    run = RunRegistry().submit(kind="chat", owner_id="operator", orchestrator=orch)
    await run.wait()
    assert _ended(run).code is None


async def test_the_loop_guard_sets_its_code(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(monkeypatch, Settings(loop_repeat_threshold=1))
    orchestrator = build_chat_orchestrator(
        "go", model=_calls_step(), categories={"work": _toolset()}
    )
    run = RunRegistry().submit(kind="chat", owner_id="operator", orchestrator=orchestrator)
    await run.wait()

    assert run.status is RunStatus.blocked
    assert _ended(run).code == "loop"


async def test_the_first_token_bound_sets_its_code(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(monkeypatch, Settings(first_token_timeout_s=0.05))

    async def stream_fn(
        _messages: list[ModelMessage], _info: AgentInfo
    ) -> AsyncIterator[str | DeltaToolCalls]:
        await asyncio.sleep(2)
        yield "late"

    orchestrator = build_chat_orchestrator(
        "go", model=FunctionModel(stream_function=stream_fn), categories={}
    )
    run = RunRegistry().submit(kind="chat", owner_id="operator", orchestrator=orchestrator)
    await run.wait()

    assert run.status is RunStatus.blocked
    assert _ended(run).code == "first_token"


async def test_exhausted_transient_retries_end_in_an_error_without_a_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings(monkeypatch, Settings(auto_continue_transient_max=1))
    monkeypatch.setattr(turn, "TRANSIENT_BACKOFF_S", (0.0,))

    async def stream_fn(
        _messages: list[ModelMessage], _info: AgentInfo
    ) -> AsyncIterator[str | DeltaToolCalls]:
        for chunk in list[str]():
            yield chunk
        raise ModelHTTPError(status_code=503, model_name="m", body="overloaded")

    orchestrator = build_chat_orchestrator(
        "go", model=FunctionModel(stream_function=stream_fn), categories={}
    )
    run = RunRegistry().submit(kind="chat", owner_id="operator", orchestrator=orchestrator)
    await run.wait()

    assert run.status is RunStatus.error
    assert isinstance(run.stream.replay()[-1].body, RunError)
    assert run.end_code is None


# --- the turn cap -----------------------------------------------------------


async def test_the_request_cap_blocks_the_run_and_keeps_the_partial_turn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _settings(monkeypatch, Settings())
    _, store = await make_stores(tmp_path)
    await store.start()
    conv = await store.create_conversation("operator", title="t")

    # Distinct arguments each request, so the loop guard stays out of it.
    async def stream_fn(
        messages: list[ModelMessage], _info: AgentInfo
    ) -> AsyncIterator[str | DeltaToolCalls]:
        yield {0: DeltaToolCall(name="work_step", json_args=json.dumps({"n": len(messages)}))}

    orchestrator = build_chat_orchestrator(
        "do the work",
        model=FunctionModel(stream_function=stream_fn),
        categories={"work": _toolset()},
        store=store,
        conversation_id=conv,
        max_requests=3,
    )
    run = RunRegistry().submit(kind="chat", owner_id="operator", orchestrator=orchestrator)
    await run.wait()

    assert run.status is RunStatus.blocked
    assert run.stop_cause is StopCause.turn_limit
    assert _ended(run).code == "turn_limit"
    assert [n.limit for n in bodies_of(run, LimitNotice)] == ["turn_limit"]
    views = await store.messages_view(conv)
    assert views[0].role == "user"
    assert views[-1].role == "assistant"
    assert views[-1].blocked_reason == TURN_LIMIT_DETAIL
    await store.stop()


async def test_without_a_cap_a_long_turn_is_not_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(monkeypatch, Settings(loop_repeat_threshold=1000))
    calls = 0

    async def stream_fn(
        _messages: list[ModelMessage], _info: AgentInfo
    ) -> AsyncIterator[str | DeltaToolCalls]:
        nonlocal calls
        calls += 1
        if calls > 60:
            yield "done"
        else:
            yield {0: DeltaToolCall(name="work_step", json_args=json.dumps({"n": calls}))}

    orchestrator = build_chat_orchestrator(
        "go", model=FunctionModel(stream_function=stream_fn), categories={"work": _toolset()}
    )
    run = RunRegistry().submit(kind="chat", owner_id="operator", orchestrator=orchestrator)
    await run.wait()
    assert run.status is RunStatus.done


# --- Retry-After ------------------------------------------------------------


def _http(headers: dict[str, str] | None) -> ModelHTTPError:
    return ModelHTTPError(status_code=429, model_name="m", body="slow down", headers=headers)


def test_retry_after_in_seconds_is_the_wait() -> None:
    assert transient_wait_s(_http({"retry-after": "7"}), 1) == 7.0


def test_retry_after_as_an_http_date_is_the_wait_until_then() -> None:
    when = datetime.now(UTC) + timedelta(seconds=30)
    wait = transient_wait_s(_http({"retry-after": format_datetime(when, usegmt=True)}), 1)
    assert 27.0 <= wait <= 30.0


def test_retry_after_is_capped_at_a_minute() -> None:
    assert transient_wait_s(_http({"retry-after": "3600"}), 1) == 60.0


def test_a_date_in_the_past_waits_no_longer_than_zero() -> None:
    past = format_datetime(datetime.now(UTC) - timedelta(hours=1), usegmt=True)
    assert transient_wait_s(_http({"retry-after": past}), 1) == 0.0


@pytest.mark.parametrize("headers", [None, {}, {"retry-after": "soon"}])
def test_without_a_usable_header_the_backoff_step_is_jittered_within_twenty_percent(
    headers: dict[str, str] | None,
) -> None:
    for retries, step in ((1, 2.0), (2, 8.0), (3, 8.0)):
        waits = [transient_wait_s(_http(headers), retries) for _ in range(200)]
        assert all(step * 0.8 <= wait <= step * 1.2 for wait in waits)
        assert len(set(waits)) > 1


def test_a_transport_error_uses_the_backoff() -> None:
    assert 1.6 <= transient_wait_s(RuntimeError("cut"), 1) <= 2.4


# --- save failures are visible ----------------------------------------------


async def test_a_save_failure_is_announced_once_per_conversation_per_window(
    tmp_path: Path,
) -> None:
    from core.db import init_db, make_engine
    from core.vault import Vault

    engine_ = make_engine("sqlite:///:memory:")
    init_db(engine_)
    vault = Vault(tmp_path / "keyfile.json")
    await vault.setup("pw")
    notifications = NotificationService(engine_, vault)
    await notifications.start()
    now = 1000.0
    report = save_failure_reporter(notifications, window_s=600.0, clock=lambda: now)

    async def settle() -> None:
        for _ in range(5):
            await asyncio.sleep(0)

    report("c1")
    report("c1")
    report("c2")
    await settle()
    listed, _ = await notifications.list_notifications("operator")
    assert sorted(n.conversation_id or "" for n in listed if n.kind == "save_failed") == [
        "c1",
        "c2",
    ]

    now += 601.0
    report("c1")
    await settle()
    listed, _ = await notifications.list_notifications("operator")
    assert [n.conversation_id for n in listed if n.kind == "save_failed"].count("c1") == 2
    await notifications.stop()


async def test_the_store_calls_its_listener_when_a_write_is_dropped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, store = await make_stores(tmp_path)
    conv = await store.create_conversation("operator", title="t")
    seen: list[str] = []
    store.on_save_failed(seen.append)

    async def refuse(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("services.conversations.drainer.in_session", refuse)

    def no_wait(_base_s: float, _attempt: int) -> float:
        return 0.0

    monkeypatch.setattr("core.worker.backoff_delay", no_wait)
    await store.start()
    _ = store.record_prompt(conv, ModelRequest(parts=[UserPromptPart("hi")]))
    await store.stop()

    assert seen == [conv]


async def test_stopping_with_the_vault_locked_warns_with_the_items_left(
    caplog: pytest.LogCaptureFixture,
) -> None:
    locked = asyncio.Event()
    handled: list[int] = []

    async def handler(item: int) -> None:
        handled.append(item)

    worker = WriteBehindWorker(handler, name="test-drainer", unlocked=locked)
    await worker.start()
    worker.submit(1)
    worker.submit(2)

    with caplog.at_level(logging.WARNING, logger="core.worker"):
        await worker.stop(drain_timeout_s=0.1)

    assert handled == []
    assert any("2 item(s) left unflushed" in record.getMessage() for record in caplog.records)
