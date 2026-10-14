"""`AE-3.6` — external tools are sensitive by default, trusted one at a time.

Driven end to end against a real MCP server: the agent is composed exactly as a chat turn
composes it, and what is asserted is the behaviour the requirement names — an untrusted
tool *parks* for approval, a trusted one *runs*, trust granted to one tool leaves its
sibling on the same server parked, and revoking it puts the tool straight back to parking.
"""

from __future__ import annotations

import json
import sys
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

from pydantic_ai import Agent, DeferredToolRequests, ModelMessage
from pydantic_ai.models.function import (
    AgentInfo,
    DeltaToolCall,
    DeltaToolCalls,
    FunctionModel,
    StreamFunctionDef,
)

from agent import stream_agent_run
from core.container import ServiceContainer
from core.db import init_db, make_engine
from core.vault import Vault
from runs import Run, RunStream
from services.external_tools import ExternalTools, build_external_tools
from services.mcp.registry import STATUS_CONNECTED, McpServerView
from tools import RunDeps, build_agent_toolsets
from tools.external import external_toolset

OWNER = "operator"
CONV = "conv-1"
SAMPLE_SERVER = str(Path(__file__).with_name("mcp_sample_server.py"))


async def _wired() -> tuple[ExternalTools, McpServerView]:
    """The external capability with the sample MCP server registered and connected —
    built exactly as the app builds it, so what these tests drive is the real handle."""
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(Path(tempfile.mkdtemp()) / "keyfile.json")
    await vault.setup("pw")
    external = build_external_tools(engine, vault)
    view = await external.mcp.register(
        OWNER, name="Sample", transport="stdio", command=sys.executable, args=[SAMPLE_SERVER]
    )
    assert view.status == STATUS_CONNECTED, view.last_error
    return external, view


def _agent(stream_fn: StreamFunctionDef) -> Agent[RunDeps, DeferredToolRequests | str]:
    """The agent a chat turn builds, narrowed to the external category — same toolset
    stack, same ``output_type``, so approval really defers instead of erroring."""
    return Agent(
        FunctionModel(stream_function=stream_fn),
        deps_type=RunDeps,
        toolsets=build_agent_toolsets({"external": external_toolset()}),
        output_type=[str, DeferredToolRequests],
    )


async def _run(
    agent: Agent[RunDeps, DeferredToolRequests | str], external: ExternalTools | None, run_id: str
) -> tuple[DeferredToolRequests | str, Run]:
    run = Run(id=run_id, kind="chat", owner_id=OWNER, stream=RunStream())
    caps = ServiceContainer.of(external) if external is not None else ServiceContainer()
    deps = RunDeps(run=run, owner_id=OWNER, conversation_id=CONV, caps=caps)
    async with agent.iter("go", deps=deps) as agent_run:
        await stream_agent_run(agent_run, run)
        assert agent_run.result is not None
        return agent_run.result.output, run


def _call_once(tool_name: str, args: dict[str, object]) -> StreamFunctionDef:
    """A model that calls one tool once, then answers with text once it has run."""

    def _tool_ran(messages: list[ModelMessage]) -> bool:
        return any(
            type(part).__name__ == "ToolReturnPart"
            for message in messages
            for part in message.parts
        )

    async def stream_fn(
        messages: list[ModelMessage], _info: AgentInfo
    ) -> AsyncIterator[str | DeltaToolCalls]:
        if _tool_ran(messages):
            yield "done"
        else:
            yield {0: DeltaToolCall(name=tool_name, json_args=json.dumps(args))}

    return stream_fn


async def _drive(external: ExternalTools, tool_name: str, args: dict[str, object]):
    """Compose the external category the way a chat turn does and run one turn."""
    return await _run(_agent(_call_once(tool_name, args)), external, "r1")


async def _offered_tools(external: ExternalTools | None) -> list[str]:
    """The catalog the model is actually handed for a turn — read off the model call
    itself rather than reconstructed, so it is exactly what the agent offered."""
    seen: list[str] = []

    async def stream_fn(
        _messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str | DeltaToolCalls]:
        seen.extend(sorted(t.name for t in info.function_tools))
        yield "done"

    _ = await _run(_agent(stream_fn), external, "catalog")
    return seen


def _types(run: Run) -> list[str]:
    return [e.body.type for e in run.stream.replay()]


async def test_a_discovered_tool_is_offered_under_its_servers_namespace():
    external, view = await _wired()

    names = set(await _offered_tools(external))

    # The server's slug namespaces its tools inside the `external` category, so two
    # servers exposing `echo` stay distinguishable to the model and to approval.
    assert f"external_{view.slug}_echo" in names
    assert f"external_{view.slug}_add" in names


async def test_an_untrusted_external_tool_parks_for_approval():
    external, view = await _wired()

    out, run = await _drive(external, f"external_{view.slug}_echo", {"text": "hi"})

    # An external tool's effects are unknown to the system, so the default is to ask.
    assert isinstance(out, DeferredToolRequests)
    assert [c.tool_name for c in out.approvals] == [f"external_{view.slug}_echo"]
    assert "tool.completed" not in _types(run)


async def test_a_trusted_external_tool_runs_without_asking():
    external, view = await _wired()
    _ = await external.mcp.set_tool_policy(OWNER, view.id, "echo", trusted=True)

    out, run = await _drive(external, f"external_{view.slug}_echo", {"text": "hi"})

    assert not isinstance(out, DeferredToolRequests)
    assert "tool.completed" in _types(run)


async def test_trust_is_per_tool_so_a_sibling_on_the_same_server_still_parks():
    """Registering or enabling a server must never blanket-trust what it exposes."""
    external, view = await _wired()
    _ = await external.mcp.set_tool_policy(OWNER, view.id, "echo", trusted=True)

    out, run = await _drive(external, f"external_{view.slug}_add", {"a": 1, "b": 2})

    assert isinstance(out, DeferredToolRequests)
    assert [c.tool_name for c in out.approvals] == [f"external_{view.slug}_add"]
    assert "tool.completed" not in _types(run)


async def test_revoking_trust_returns_the_tool_to_parking():
    external, view = await _wired()
    _ = await external.mcp.set_tool_policy(OWNER, view.id, "echo", trusted=True)
    out, _run = await _drive(external, f"external_{view.slug}_echo", {"text": "hi"})
    assert not isinstance(out, DeferredToolRequests)

    _ = await external.mcp.set_tool_policy(OWNER, view.id, "echo", trusted=False)

    out, run = await _drive(external, f"external_{view.slug}_echo", {"text": "hi"})
    assert isinstance(out, DeferredToolRequests)
    assert [c.tool_name for c in out.approvals] == [f"external_{view.slug}_echo"]
    assert "tool.completed" not in _types(run)


async def test_a_disabled_tool_is_not_offered_at_all():
    """`MCP-1`/`AE-3.3` — disabled is a stronger statement than untrusted: the tool is
    never put in front of the model, so there is nothing to approve."""
    external, view = await _wired()
    _ = await external.mcp.set_tool_policy(OWNER, view.id, "echo", enabled=False)

    names = set(await _offered_tools(external))

    assert f"external_{view.slug}_echo" not in names
    assert f"external_{view.slug}_add" in names


async def test_without_the_capability_the_category_is_empty():
    """An unwired handle degrades like every other absent capability: no tools, no
    error — never a turn that fails because the operator has no servers."""
    assert await _offered_tools(None) == []


async def test_with_nothing_registered_the_category_is_empty():
    """No servers, no connectors ⇒ the catalog the model sees is exactly what it was."""
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(Path(tempfile.mkdtemp()) / "keyfile.json")
    await vault.setup("pw")

    assert await _offered_tools(build_external_tools(engine, vault)) == []


async def test_a_connector_action_rides_the_same_gate():
    """`INTEG-2` — an agent call to a configured integration is the same kind of unknown
    as an MCP tool, so it gates identically and is trusted the same way."""
    external, _view = await _wired()
    connector = await external.integrations.configure(OWNER, "github", credentials={"token": "t"})

    out, run = await _drive(
        external, f"external_{connector.slug}_get_repo", {"params": {"owner": "a", "repo": "b"}}
    )
    assert isinstance(out, DeferredToolRequests)
    assert [c.tool_name for c in out.approvals] == [f"external_{connector.slug}_get_repo"]
    assert "tool.completed" not in _types(run)

    # Trusting one action leaves the connector's others parked.
    _ = await external.integrations.set_action_policy(OWNER, connector.id, "get_repo", trusted=True)
    out, _run = await _drive(
        external,
        f"external_{connector.slug}_list_issues",
        {"params": {"owner": "a", "repo": "b"}},
    )
    assert isinstance(out, DeferredToolRequests)
