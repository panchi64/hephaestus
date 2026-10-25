"""``tools.call``: a window running one of its own plugin's published tools.

The tool runs exactly as the agent's call would: in the plugin's VM, reaching only its
declared hosts, with its secrets. A tool that declares an external effect asks the
operator first.
"""

from __future__ import annotations

from pydantic import JsonValue

from core.exceptions import InvalidInputError, NotFoundError
from core.serde import parse_json
from services.tool_sensitivity import Sensitivity

from ..calls import argument_problem, plugin_secrets
from ..manifest import PluginManifest, PluginTool
from ..runtime import PluginError, PluginRuntime
from ..secrets import PluginSecretStore
from ..store import PluginStore
from .base import BridgeCall, BridgeCapability, Confirmation
from .registry import BRIDGE

_NAME_MAX_CHARS = 64


async def _resolve(call: BridgeCall) -> tuple[PluginManifest, PluginTool]:
    view = await call.services.get(PluginStore).get(call.window.owner_id, call.window.plugin_id)
    name = call.text("name", max_chars=_NAME_MAX_CHARS)
    tool = None if view.active is None else view.active.tool(name)
    if view.active is None or not view.published or tool is None:
        raise NotFoundError(f"{call.window.plugin} has no published tool {name!r}")
    return view.active, tool


async def _asks(call: BridgeCall) -> Confirmation | None:
    _manifest, tool = await _resolve(call)
    if tool.sensitivity is not Sensitivity.EXTERNAL_EFFECT:
        return None
    return Confirmation(summary=f"run its {tool.name} tool", grant=f"tools.call:{tool.name}")


async def _call(call: BridgeCall) -> JsonValue:
    manifest, tool = await _resolve(call)
    args = call.params.get("args") or {}
    if not isinstance(args, dict):
        raise InvalidInputError("`args` is an object")
    problem = argument_problem(tool, args)
    if problem is not None:
        raise InvalidInputError(f"those arguments do not fit {tool.name}: {problem}")
    runtime = call.services.get_optional(PluginRuntime)
    if runtime is None or not runtime.available:
        raise PluginError("plugin tools run in a VM, which is not running right now")
    owner, plugin_id = call.window.owner_id, call.window.plugin_id
    store = call.services.get(PluginStore)
    secrets = await plugin_secrets(call.services.get(PluginSecretStore), owner, plugin_id, manifest)
    result = await runtime.call(
        manifest,
        await store.files(owner, plugin_id, "active"),
        tool,
        args,
        hosts=manifest.egress,
        secret_values=secrets,
    )
    if not result.ok:
        raise PluginError(result.output)
    try:
        return parse_json(result.output)
    except ValueError:
        return result.output


BRIDGE.register(BridgeCapability("tools.call", "tools", _call, asks=_asks))
