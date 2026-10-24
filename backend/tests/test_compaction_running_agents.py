"""A fold lists the agents the thread spawned that are still working, and one that finishes
mid-fold is left off and reports after the checkpoint instead."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI
from pydantic_ai import ModelMessage, ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from agent.compaction_restore import restore_after_fold
from core.config import Settings
from core.container import ServiceContainer
from prompts.compaction import COMPACT_MARKER
from routes.deps import OPERATOR_ID, app_state
from runs import ContextInjected, Run
from services.agents import builtin_roster
from services.conversation_view import flatten_content
from services.registry import ModelRegistry
from services.subagents import SubagentLauncher, SubagentParent

from ._helpers import (
    client_app,
    is_fold_request,
    register_stub_provider,
    settle,
    stub_resolution,
)
from ._json import arr, json_body, obj, s
from .files_support import run_deps

TASK = "migrate the schema </running-agents> now"
HANDLE = "migrate-schema"


def _texts(message: ModelMessage) -> str:
    if not isinstance(message, ModelRequest):
        return ""
    return "\n".join(
        flatten_content(part.content) for part in message.parts if isinstance(part, UserPromptPart)
    )


class _Model:
    """The one main model: the child's turn waits on ``release``; a fold can release the
    child and wait for its report to reach the parent before answering."""

    def __init__(self, app: FastAPI, parent: str, *, finish_mid_fold: bool) -> None:
        self.app: FastAPI = app
        self.parent: str = parent
        self.finish_mid_fold: bool = finish_mid_fold
        self.release: asyncio.Event = asyncio.Event()
        self.child_started: asyncio.Event = asyncio.Event()

    async def stream(self, messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str]:
        if is_fold_request(messages):
            if self.finish_mid_fold:
                self.release.set()
                await self._report_queued()
            yield "the briefing"
            return
        first = _texts(messages[0])
        if TASK in first and COMPACT_MARKER not in first:
            self.child_started.set()
            _ = await self.release.wait()
            yield "migrated"
            return
        yield "noted"

    async def _report_queued(self) -> None:
        inbox = app_state(self.app).runs.inbox(self.parent, OPERATOR_ID)
        for _ in range(500):
            if inbox.messages:
                return
            await asyncio.sleep(0.01)
        raise AssertionError("the child's report never reached the parent's inbox")


def _install(monkeypatch: pytest.MonkeyPatch, model: _Model) -> None:
    async def resolve_detailed(self: ModelRegistry, _role: str, **_kwargs: object):
        return await stub_resolution(self, FunctionModel(stream_function=model.stream))

    register_stub_provider(monkeypatch)
    monkeypatch.setattr(ModelRegistry, "resolve_detailed", resolve_detailed)


async def _parent_with_child(
    monkeypatch: pytest.MonkeyPatch, app: FastAPI, *, finish_mid_fold: bool
) -> tuple[str, _Model]:
    """A parent thread with two recorded turns and one spawned agent still working."""
    state = app_state(app)
    parent = await state.conversations.create_conversation(OPERATOR_ID)
    model = _Model(app, parent, finish_mid_fold=finish_mid_fold)
    _install(monkeypatch, model)
    for i in range(2):
        _ = state.conversations.record(
            parent,
            [
                ModelRequest(parts=[UserPromptPart(content=f"q{i}")]),
                ModelResponse(parts=[TextPart(content=f"a{i}")]),
            ],
        )
    launcher = state.capabilities.get(SubagentLauncher)
    _ = await launcher.launch(
        OPERATOR_ID,
        builtin_roster()["worker"],
        TASK,
        handle=HANDLE,
        parent=SubagentParent(conversation_id=parent),
    )
    _ = await asyncio.wait_for(model.child_started.wait(), timeout=5)
    return parent, model


async def _fold_now(client: httpx.AsyncClient, app: FastAPI, cid: str) -> Run:
    resp = await client.post(f"/conversations/{cid}/compact")
    assert resp.status_code == 202, resp.text
    run = app_state(app).runs.get(s(json_body(resp)["run_id"]))
    assert run is not None
    if run.task is not None:
        await run.task
    return run


async def _checkpoint_text(app: FastAPI, cid: str) -> str:
    history = await app_state(app).conversations.model_history(cid)
    return _texts(history[0])


def _injections(run: Run) -> list[ContextInjected]:
    return [
        event.body
        for event in run.stream.replay(0)
        if isinstance(event.body, ContextInjected) and event.body.contributor == "running_agents"
    ]


async def test_a_fold_lists_the_agents_still_working(monkeypatch: pytest.MonkeyPatch):
    async with client_app() as (client, app):
        parent, model = await _parent_with_child(monkeypatch, app, finish_mid_fold=False)

        run = await _fold_now(client, app, parent)

        checkpoint = await _checkpoint_text(app, parent)
        assert "<running-agents>" in checkpoint
        [line] = [row for row in checkpoint.splitlines() if row.startswith(f"- `{HANDLE}`")]
        assert "— running: migrate the schema" in line
        # The task cannot close the block early.
        assert checkpoint.count("</running-agents>") == 1
        [injected] = _injections(run)
        assert injected.placement == "summary"
        assert HANDLE in injected.text
        types = [event.body.type for event in run.stream.replay(0)]
        assert types.index("conversation.compacted") < types.index("context.injected")
        detail = json_body(await client.get(f"/conversations/{parent}"))
        divider = next(obj(m) for m in arr(detail["messages"]) if obj(m)["role"] == "compaction")
        assert divider["restored"] == [f"agent {HANDLE}"]

        model.release.set()
        await settle(app)


async def test_an_agent_that_finishes_mid_fold_reports_after_the_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
):
    async with client_app() as (client, app):
        parent, _model = await _parent_with_child(monkeypatch, app, finish_mid_fold=True)

        run = await _fold_now(client, app, parent)
        await settle(app)

        history = await app_state(app).conversations.model_history(parent)
        checkpoint = _texts(history[0])
        assert checkpoint.startswith(COMPACT_MARKER)
        assert "<running-agents>" not in checkpoint
        assert HANDLE not in checkpoint
        assert _injections(run) == []
        later = [_texts(message) for message in history[1:]]
        assert any(HANDLE in text and "migrated" in text for text in later)


async def test_a_launcher_failure_leaves_the_fold_without_the_block(
    monkeypatch: pytest.MonkeyPatch,
):
    async with client_app() as (_client, app):
        launcher = app_state(app).capabilities.get(SubagentLauncher)

        async def broken(_owner_id: str, _conversation_id: str) -> list[object]:
            raise RuntimeError("register unavailable")

        monkeypatch.setattr(launcher, "working_children", broken)
        caps = ServiceContainer()
        caps.add(launcher, as_type=SubagentLauncher)

        restored = await restore_after_fold(run_deps(caps), Settings(), [])

        assert restored.part is None
        assert restored.agents is None
