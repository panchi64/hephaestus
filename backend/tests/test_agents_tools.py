"""The agent-facing `agents` tools, and the enabled gate's reading of an agent's tools."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import JsonValue
from pydantic_ai import RunContext
from pydantic_ai.exceptions import ApprovalRequired, ModelRetry
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from core.container import ServiceContainer
from runs import Run, RunStream
from services.agents import UNRESTRICTED, AgentStore, ToolAccess
from tools import build_agent_toolsets
from tools.agents import agents_toolset
from tools.builtin import builtin_toolset
from tools.deps import RunDeps
from tools.files import files_toolset

from ._agent_helpers import OWNER, agent_values, make_stores
from ._json import JSON, obj


def _run() -> Run:
    return Run(id="run-1", kind="chat", owner_id=OWNER, stream=RunStream())


def _ctx(
    store: AgentStore | None, *, tool_access: ToolAccess = UNRESTRICTED
) -> RunContext[RunDeps]:
    caps = ServiceContainer()
    if store is not None:
        caps.add(store)
    return RunContext(
        deps=RunDeps(run=_run(), owner_id=OWNER, caps=caps, tool_access=tool_access),
        model=TestModel(),
        usage=RunUsage(),
    )


async def _call(
    ctx: RunContext[RunDeps], name: str, args: dict[str, object]
) -> dict[str, JsonValue]:
    toolset = agents_toolset()
    tools = await toolset.get_tools(ctx)
    return obj(JSON.validate_python(await toolset.call_tool(name, args, ctx, tools[name])))


_WRITE: dict[str, object] = {
    "name": "scout",
    "description": "Looks around.",
    "instructions": "Look around.",
}


@pytest.fixture
async def store(tmp_path: Path) -> AgentStore:
    return (await make_stores(tmp_path))[0]


async def test_writing_creates_a_draft_attributed_to_the_agent(store: AgentStore):
    result = await _call(_ctx(store), "write", _WRITE)
    assert result == {"ok": True, "name": "scout", "status": "draft"}
    stored = await store.get_by_name(OWNER, "scout")
    assert stored is not None
    assert (stored.source, stored.published) == ("agent", False)


async def test_the_tool_lists_become_tool_access(store: AgentStore):
    _ = await _call(_ctx(store), "write", {**_WRITE, "tools": ["files"], "deny_tools": ["shell"]})
    stored = await store.get_by_name(OWNER, "scout")
    assert stored is not None
    assert stored.tools == ToolAccess(allow=frozenset({"files"}), deny=frozenset({"shell"}))


async def test_rewriting_its_own_draft_needs_no_approval(store: AgentStore):
    _ = await _call(_ctx(store), "write", _WRITE)
    _ = await _call(_ctx(store), "write", {**_WRITE, "description": "Looks harder."})
    stored = await store.get_by_name(OWNER, "scout")
    assert stored is not None and stored.description == "Looks harder."


async def test_an_operators_own_draft_is_left_alone(store: AgentStore):
    _ = await store.create(OWNER, agent_values("scout"), source="user")
    result = await _call(_ctx(store), "write", _WRITE)
    assert result["ok"] is False
    stored = await store.get_by_name(OWNER, "scout")
    assert stored is not None and stored.description == "Helps with things."


async def test_rewriting_a_published_agent_asks_first(store: AgentStore):
    _ = await store.create(OWNER, agent_values("scout"), source="user", published=True)
    with pytest.raises(ApprovalRequired):
        _ = await _call(_ctx(store), "write", _WRITE)
    stored = await store.get_by_name(OWNER, "scout")
    assert stored is not None and stored.description == "Helps with things."


async def test_an_approved_rewrite_of_a_published_agent_goes_through(store: AgentStore):
    _ = await store.create(OWNER, agent_values("scout"), source="user", published=True)
    ctx = _ctx(store)
    ctx.tool_call_approved = True
    _ = await _call(ctx, "write", _WRITE)
    stored = await store.get_by_name(OWNER, "scout")
    assert stored is not None and stored.description == "Looks around."


async def test_an_invalid_agent_is_sent_back_to_the_model(store: AgentStore):
    with pytest.raises(ModelRetry, match="description"):
        _ = await _call(_ctx(store), "write", {**_WRITE, "description": " "})


async def test_publish_publishes(store: AgentStore):
    _ = await _call(_ctx(store), "write", _WRITE)
    ctx = _ctx(store)
    ctx.tool_call_approved = True
    result = await _call(ctx, "publish", {"name": "scout"})
    assert result["status"] == "published"
    stored = await store.get_by_name(OWNER, "scout")
    assert stored is not None and stored.published


async def test_publish_is_gated_on_approval(store: AgentStore):
    toolset = agents_toolset()
    tools = await toolset.get_tools(_ctx(store))
    assert tools["publish"].tool_def.kind == "unapproved"


async def test_publishing_something_unwritten_is_sent_back(store: AgentStore):
    with pytest.raises(ModelRetry):
        _ = await _call(_ctx(store), "publish", {"name": "nothing"})


async def test_without_a_store_the_tools_say_so():
    assert (await _call(_ctx(None), "write", _WRITE))["ok"] is False


async def _offered(ctx: RunContext[RunDeps]) -> set[str]:
    stack = build_agent_toolsets({"files": files_toolset(), "builtin": builtin_toolset()})[0]
    return set(await stack.get_tools(ctx))


async def test_the_enabled_gate_honours_the_agents_tool_access():
    open_ = await _offered(_ctx(None))
    assert any(name.startswith("files_") for name in open_)
    assert any(name.startswith("builtin_") for name in open_)

    only_files = await _offered(_ctx(None, tool_access=ToolAccess(allow=frozenset({"files"}))))
    assert only_files == {name for name in open_ if name.startswith("files_")}

    denied = await _offered(_ctx(None, tool_access=ToolAccess(deny=frozenset({"builtin"}))))
    assert denied == {name for name in open_ if not name.startswith("builtin_")}

    one = await _offered(_ctx(None, tool_access=ToolAccess(deny=frozenset({"files_write_file"}))))
    assert "files_write_file" not in one and "files_read_file" in one
