"""Running a plugin's tool in its VM, shared by the agent's tools and the HTTP bridge."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from jsonschema import Draft202012Validator
from jsonschema.protocols import Validator
from pydantic import JsonValue

from core.diagnostics import render_tool_error
from core.untrusted import wrap_untrusted
from services.revisions import Bundle
from services.vm import VMError

from .manifest import PluginManifest, PluginTool
from .runtime import PluginError, PluginRuntime
from .secrets import PluginSecretStore, SecretValue

_NO_VM = render_tool_error(
    "plugins run in a VM, which is not running right now",
    hints=["carry on without this plugin, and tell the operator if the task needs it"],
)


def argument_problem(tool: PluginTool, args: JsonValue) -> str | None:
    """The first way ``args`` breaks the tool's schema, in a stable order; None if none."""
    return _first_problem(Draft202012Validator(tool.parameters), args)


def _first_problem(validator: Validator, args: JsonValue) -> str | None:
    errors = sorted(validator.iter_errors(args), key=str)
    return errors[0].message if errors else None


async def plugin_secrets(
    secrets: PluginSecretStore, owner_id: str, plugin_id: str, manifest: PluginManifest
) -> dict[str, SecretValue]:
    """The operator's values for the secrets ``manifest`` declares."""
    values = await secrets.secret_values(owner_id, plugin_id)
    return {name: value for name, value in values.items() if name in manifest.secrets}


async def run_plugin_tool(
    runtime: PluginRuntime,
    manifest: PluginManifest,
    files: Bundle,
    tool: PluginTool,
    args: Mapping[str, object],
    hosts: Iterable[str],
    secrets: Mapping[str, SecretValue],
) -> dict[str, object]:
    """Run one tool in the plugin's VM and shape its outcome as an untrusted reply."""
    if not runtime.available:
        return {"ok": False, "error": _NO_VM}
    try:
        result = await runtime.call(manifest, files, tool, args, hosts=hosts, secret_values=secrets)
    except (PluginError, VMError) as exc:
        return {"ok": False, "error": str(exc)}
    source = f"plugin {manifest.name}"
    reply: dict[str, object] = {"ok": result.ok}
    if result.ok:
        reply["result"] = wrap_untrusted(result.output, source=source)
    else:
        reply["error"] = wrap_untrusted(result.output, source=source)
    if result.log.strip():
        reply["log"] = wrap_untrusted(result.log, source=source)
    return reply
