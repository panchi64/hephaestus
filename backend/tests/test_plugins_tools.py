"""The agent-facing `plugins` tools: drafting, testing, and the tools published plugins add."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import pytest
from pydantic_ai import RunContext
from pydantic_ai.exceptions import ApprovalRequired, ModelRetry
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from agent.factory import build_agent
from core.container import ServiceContainer
from runs import Run, RunStream
from services.plugins import (
    CallResult,
    PluginManifest,
    PluginRuntime,
    PluginSecretStore,
    PluginStore,
    SecretValue,
)
from services.plugins.manifest import PluginTool
from services.tool_sensitivity import SENSITIVITY_METADATA_KEY
from tools.deps import RunDeps
from tools.plugins import plugins_toolset

from ._plugin_helpers import MANIFEST, OWNER, bundle, make_plugin_stores


@pytest.fixture
async def stores(tmp_path: Path) -> tuple[PluginStore, PluginSecretStore]:
    return await make_plugin_stores(tmp_path)


@pytest.fixture
def store(stores: tuple[PluginStore, PluginSecretStore]) -> PluginStore:
    return stores[0]


@pytest.fixture
def secrets(stores: tuple[PluginStore, PluginSecretStore]) -> PluginSecretStore:
    return stores[1]


@dataclass
class _Call:
    tool: str
    args: Mapping[str, object]
    hosts: list[str]
    secrets: dict[str, SecretValue]


@dataclass
class _Runtime:
    """Stands in for the VM runtime, recording what each call was given."""

    available: bool = True
    calls: list[_Call] = field(default_factory=list)

    async def call(
        self,
        _manifest: PluginManifest,
        _files: Mapping[str, bytes],
        tool: PluginTool,
        args: Mapping[str, object],
        *,
        hosts: Iterable[str],
        secret_values: Mapping[str, SecretValue],
        **_: object,
    ) -> CallResult:
        self.calls.append(_Call(tool.name, args, list(hosts), dict(secret_values)))
        return CallResult(True, '{"sky": "clear"}', "")


def _ctx(
    store: PluginStore,
    secrets: PluginSecretStore,
    runtime: _Runtime | None = None,
    *,
    approved: bool = False,
) -> RunContext[RunDeps]:
    caps = ServiceContainer()
    caps.add(store)
    caps.add(secrets)
    if runtime is not None:
        caps.add(runtime, as_type=PluginRuntime)
    return RunContext(
        deps=RunDeps(
            run=Run(id="run-1", kind="chat", owner_id=OWNER, stream=RunStream()),
            owner_id=OWNER,
            caps=caps,
        ),
        model=TestModel(),
        usage=RunUsage(),
        tool_call_approved=approved,
    )


async def _call(ctx: RunContext[RunDeps], name: str, args: dict[str, object]) -> dict[str, object]:
    toolset = plugins_toolset()
    tools = await toolset.get_tools(ctx)
    return cast(dict[str, object], await toolset.call_tool(name, args, ctx, tools[name]))


def _texts() -> dict[str, str]:
    return {path: blob.decode() for path, blob in bundle().items()}


async def test_writing_drafts_a_plugin_and_names_its_unset_secrets(
    store: PluginStore, secrets: PluginSecretStore
):
    result = await _call(_ctx(store, secrets), "write", {"name": "weather", "files": _texts()})
    assert result == {
        "ok": True,
        "name": "weather",
        "status": "draft",
        "tools": ["forecast"],
        "windows": [],
        "secrets_unset": ["API_KEY"],
    }


async def test_a_broken_plugin_is_sent_back_to_fix(store: PluginStore, secrets: PluginSecretStore):
    files = {**_texts(), "plugin.yaml": MANIFEST.replace("weather:forecast", "nope:forecast")}
    with pytest.raises(ModelRetry):
        _ = await _call(_ctx(store, secrets), "write", {"name": "weather", "files": files})


async def test_an_operators_own_draft_is_left_alone(store: PluginStore, secrets: PluginSecretStore):
    _ = await store.write_draft(OWNER, "weather", bundle(), source="user")
    result = await _call(_ctx(store, secrets), "write", {"name": "weather", "files": _texts()})
    assert result["ok"] is False


async def test_testing_runs_the_draft_without_asking_when_no_secret_is_sent(
    store: PluginStore, secrets: PluginSecretStore
):
    runtime = _Runtime()
    _ = await store.write_draft(OWNER, "weather", bundle(), source="agent")
    result = await _call(
        _ctx(store, secrets, runtime),
        "test",
        {"name": "weather", "tool": "forecast", "args": {"city": "Oslo"}},
    )
    assert result["ok"] is True
    assert "clear" in str(result["result"])
    assert runtime.calls[0].args == {"city": "Oslo"}
    # No egress policy here, so nothing beyond the conversation's own reach.
    assert runtime.calls[0].hosts == []


async def test_testing_with_the_operators_secrets_asks_first(
    store: PluginStore, secrets: PluginSecretStore
):
    runtime = _Runtime()
    view = await store.write_draft(OWNER, "weather", bundle(), source="agent")
    await secrets.set_secret(OWNER, view.id, "API_KEY", "s3cret")
    args: dict[str, object] = {"name": "weather", "tool": "forecast", "args": {"city": "Oslo"}}
    with pytest.raises(ApprovalRequired):
        _ = await _call(_ctx(store, secrets, runtime), "test", args)

    _ = await _call(_ctx(store, secrets, runtime, approved=True), "test", args)
    assert runtime.calls[0].secrets["API_KEY"].value == "s3cret"


async def test_a_published_plugins_tools_are_offered_with_their_declared_class(
    store: PluginStore, secrets: PluginSecretStore
):
    view = await store.write_draft(OWNER, "weather", bundle(), source="agent")
    ctx = _ctx(store, secrets, _Runtime())
    toolset = plugins_toolset()
    assert "weather_forecast" not in await toolset.get_tools(ctx)

    _ = await store.publish(OWNER, view.id)
    toolset = plugins_toolset()
    tool = (await toolset.get_tools(ctx))["weather_forecast"]
    assert tool.tool_def.metadata == {SENSITIVITY_METADATA_KEY: "read"}
    assert tool.tool_def.description == "weather: The forecast for a city."


async def test_a_published_tool_runs_with_its_declared_hosts_and_checked_arguments(
    store: PluginStore, secrets: PluginSecretStore
):
    runtime = _Runtime()
    view = await store.write_draft(OWNER, "weather", bundle(), source="agent")
    _ = await store.publish(OWNER, view.id)
    ctx = _ctx(store, secrets, runtime)
    with pytest.raises(ModelRetry):
        _ = await _call(ctx, "weather_forecast", {"town": "Oslo"})

    result = await _call(ctx, "weather_forecast", {"city": "Oslo"})
    assert result["ok"] is True
    assert runtime.calls[0].hosts == ["api.example.com"]


async def test_no_plugin_tools_are_offered_while_the_vm_is_down(
    store: PluginStore, secrets: PluginSecretStore
):
    view = await store.write_draft(OWNER, "weather", bundle(), source="agent")
    _ = await store.publish(OWNER, view.id)
    tools = await plugins_toolset().get_tools(_ctx(store, secrets, _Runtime(available=False)))
    assert set(tools) == {"write", "read", "test", "open_window"}


async def test_a_turn_loads_the_plugins_group_and_calls_a_published_tool(
    store: PluginStore, secrets: PluginSecretStore
):
    runtime = _Runtime()
    view = await store.write_draft(OWNER, "weather", bundle(), source="agent")
    _ = await store.publish(OWNER, view.id)
    categories = {"plugins": plugins_toolset()}
    dormant = {"plugins": "the operator's published plugins"}
    offered: list[set[str]] = []

    def respond(_messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        offered.append({tool.name for tool in info.function_tools})
        if len(offered) == 1:
            return ModelResponse(parts=[ToolCallPart("search_tools", {"queries": ["plugins"]})])
        if len(offered) == 2:
            return ModelResponse(parts=[ToolCallPart("plugins_weather_forecast", {"city": "Oslo"})])
        return ModelResponse(parts=[TextPart("done")])

    agent = build_agent(FunctionModel(respond), categories=categories, dormant=dormant)
    _ = await agent.run("forecast?", deps=_ctx(store, secrets, runtime).deps)

    assert "plugins_weather_forecast" not in offered[0]
    assert "plugins_weather_forecast" in offered[1]
    assert runtime.calls[0].args == {"city": "Oslo"}
