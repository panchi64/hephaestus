"""The `plugins` category: the model writing plugins, and the tools published ones add.

**What an agent writes is a draft.** It runs only through ``plugins_test``, in the
platform VM, reaching only the hosts this conversation may already reach. The operator
reads the draft and publishes it; only then do its tools join this category,
each gated by the class its manifest declares and reaching only its declared hosts.

A test that would send the operator's secrets asks first: a draft's code is whatever
the agent wrote, and a secret is the operator's authority at its host.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal, cast, override

from jsonschema import Draft202012Validator
from jsonschema.protocols import Validator
from pydantic import JsonValue
from pydantic_ai import AbstractToolset, FunctionToolset, ModelRetry, RunContext, Tool
from pydantic_ai.exceptions import ApprovalRequired
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import ToolsetTool, WrapperToolset
from pydantic_core import SchemaValidator, core_schema

from core.untrusted import wrap_untrusted
from services.egress import EgressPolicy
from services.plugins import (
    PluginError,
    PluginManifest,
    PluginRuntime,
    PluginStore,
    PluginTool,
    PluginValidationError,
    SecretValue,
)
from services.revisions import Bundle
from services.tool_sensitivity import SENSITIVITY_METADATA_KEY
from services.vm import VMError

from .deps import RunDeps
from .describe import registered_tools

#: Raised for approval only when a test would send the operator's secrets.
GATED_TOOLS: frozenset[str] = frozenset({"plugins_test"})

_UNAVAILABLE = "Plugins cannot be written in this deployment."
_NO_VM = "Plugins run in the platform VM, which is not running right now."
_READ_MAX_CHARS = 200_000
_ANY_ARGS = SchemaValidator(core_schema.any_schema())


def plugins_toolset() -> AbstractToolset[RunDeps]:
    toolset = FunctionToolset[RunDeps]()

    @toolset.tool
    async def write(
        ctx: RunContext[RunDeps],
        name: str,
        files: dict[str, str],
        remove: list[str] | None = None,
    ) -> dict[str, object]:
        """Draft a plugin (tools written in Python that run in the platform VM) or
        revise its draft. `files` maps paths to their full new text; `remove` deletes.

        `plugin.yaml` sits beside the modules:

            name: weather
            description: Forecasts.
            requirements: [httpx]          # pip lines
            egress: [api.example.com]      # the only hosts it can reach
            secrets: {API_KEY: api.example.com}
            tools:
              - name: forecast
                description: The forecast for a city.
                entry: weather:forecast    # module:function
                sensitivity: read          # or workspace_write, external_effect
                parameters: {type: object, properties: {city: {type: string}}}

        A tool function takes its parameters as keyword arguments (it may be async) and
        returns JSON-able data. A secret's env var holds a placeholder that becomes the
        value only in the URL or headers of HTTPS requests to its host.

        `agents/<name>.md` and `skills/<name>/SKILL.md` add agents and skills.

        It stays a draft until the operator publishes it; check it with `plugins_test`.
        """
        store = ctx.deps.caps.get_optional(PluginStore)
        if store is None:
            return {"ok": False, "error": _UNAVAILABLE}
        existing = await store.get_by_name(ctx.deps.owner_id, name.strip().lower())
        if existing is not None and existing.source == "user" and existing.draft is not None:
            return {
                "ok": False,
                "error": "That draft is the operator's own; leave it for them to finish.",
            }
        changes: dict[str, bytes | None] = {path: text.encode() for path, text in files.items()}
        changes.update({path: None for path in remove or ()})
        try:
            view = await store.write_draft(ctx.deps.owner_id, name, changes, source="agent")
        except PluginValidationError as exc:
            raise ModelRetry(f"That plugin was rejected ({exc.field}): {exc}") from None
        manifest = view.current
        return {
            "ok": True,
            "name": view.name,
            "status": "draft",
            "tools": [tool.name for tool in manifest.tools],
            "secrets_unset": [
                s.name for s in await store.secrets(ctx.deps.owner_id, view.id) if not s.is_set
            ],
        }

    @toolset.tool
    async def read(
        ctx: RunContext[RunDeps], name: str, version: Literal["current", "published"] = "current"
    ) -> dict[str, object]:
        """A plugin's files: its draft when it has one (`current`), or the version its
        tools run (`published`)."""
        store = ctx.deps.caps.get_optional(PluginStore)
        if store is None:
            return {"ok": False, "error": _UNAVAILABLE}
        view = await store.get_by_name(ctx.deps.owner_id, name.strip().lower())
        if view is None:
            raise ModelRetry(f"There is no plugin called {name!r}.")
        which = "active" if version == "published" else "current"
        files = await store.files(ctx.deps.owner_id, view.id, which)
        texts: dict[str, str] = {}
        budget = _READ_MAX_CHARS
        for path, blob in sorted(files.items()):
            text = blob.decode(errors="replace")[:budget]
            texts[path] = text
            budget -= len(text)
        return {
            "ok": True,
            "name": view.name,
            "published": view.published,
            "has_draft": view.draft is not None,
            "files": texts,
        }

    @toolset.tool
    async def test(
        ctx: RunContext[RunDeps], name: str, tool: str, args: dict[str, object] | None = None
    ) -> dict[str, object]:
        """Run one of a plugin's tools with `args`: its draft when it has one, else the
        published version. It reaches only the hosts it declares that this conversation
        may also reach."""
        store = ctx.deps.caps.get_optional(PluginStore)
        runtime = ctx.deps.caps.get_optional(PluginRuntime)
        if store is None or runtime is None:
            return {"ok": False, "error": _UNAVAILABLE}
        view = await store.get_by_name(ctx.deps.owner_id, name.strip().lower())
        if view is None:
            raise ModelRetry(f"There is no plugin called {name!r}.")
        manifest = view.current
        chosen = manifest.tool(tool)
        if chosen is None:
            raise ModelRetry(
                f"{view.name} has no tool {tool!r}; it has "
                + f"{', '.join(t.name for t in manifest.tools)}."
            )
        secrets = await _secrets(store, ctx.deps.owner_id, view.id, manifest)
        if secrets and not ctx.tool_call_approved:
            raise ApprovalRequired()
        policy = ctx.deps.caps.get_optional(EgressPolicy)
        reachable: frozenset[str] = (
            await policy.allowed_for(ctx.deps.workspace_key) if policy else frozenset()
        )
        hosts = [host for host in manifest.egress if host in reachable]
        files = await store.files(ctx.deps.owner_id, view.id, "current")
        return await _run(runtime, manifest, files, chosen, args or {}, hosts, secrets)

    return PluginsToolset(toolset)


@dataclass(frozen=True)
class _Route:
    plugin_id: str
    manifest: PluginManifest
    files: Bundle
    tool: PluginTool


class PluginsToolset(WrapperToolset[RunDeps]):
    """The `plugins` category: writing plugins, and every published plugin's tools as
    ``plugins_<plugin>_<tool>``. Published tools are read once per turn, so a plugin
    published mid-turn is offered from the next one."""

    def __init__(self, wrapped: AbstractToolset[RunDeps]) -> None:
        super().__init__(wrapped)
        self._routes: dict[str, _Route] | None = None

    @property
    @override
    def id(self) -> str:
        return "plugins"

    @property
    def tools(self) -> Mapping[str, Tool[RunDeps]]:
        """The static registry the settings catalog enumerates."""
        return registered_tools(self.wrapped)

    @override
    async def __aexit__(self, *args: object) -> bool | None:
        self._routes = None
        return await super().__aexit__(*args)

    @override
    async def get_tools(self, ctx: RunContext[RunDeps]) -> dict[str, ToolsetTool[RunDeps]]:
        tools = await super().get_tools(ctx)
        for name, route in (await self._compose(ctx)).items():
            tools[name] = ToolsetTool(
                toolset=self,
                tool_def=ToolDefinition(
                    name=name,
                    description=f"{route.manifest.name}: {route.tool.description}",
                    parameters_json_schema=route.tool.parameters,
                    metadata={SENSITIVITY_METADATA_KEY: route.tool.sensitivity.value},
                ),
                max_retries=ctx.max_retries,
                args_validator=_ANY_ARGS,
            )
        return tools

    @override
    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, JsonValue],
        ctx: RunContext[RunDeps],
        tool: ToolsetTool[RunDeps],
    ) -> object:
        route = (self._routes or {}).get(name)
        if route is None:
            return cast(object, await super().call_tool(name, tool_args, ctx, tool))
        store = ctx.deps.caps.get_optional(PluginStore)
        runtime = ctx.deps.caps.get_optional(PluginRuntime)
        if store is None or runtime is None:
            raise ModelRetry(f"{name} is no longer available.")
        problem = _first_problem(Draft202012Validator(route.tool.parameters), tool_args)
        if problem is not None:
            raise ModelRetry(f"Those arguments do not fit {name}: {problem}")
        secrets = await _secrets(store, ctx.deps.owner_id, route.plugin_id, route.manifest)
        return await _run(
            runtime,
            route.manifest,
            route.files,
            route.tool,
            tool_args,
            route.manifest.egress,
            secrets,
        )

    async def _compose(self, ctx: RunContext[RunDeps]) -> dict[str, _Route]:
        if self._routes is not None:
            return self._routes
        store = ctx.deps.caps.get_optional(PluginStore)
        runtime = ctx.deps.caps.get_optional(PluginRuntime)
        routes: dict[str, _Route] = {}
        if store is not None and runtime is not None and runtime.available:
            for view, files in await store.published(ctx.deps.owner_id):
                if view.active is not None:
                    for tool in view.active.tools:
                        name = f"{view.name}_{tool.name}"
                        routes[name] = _Route(view.id, view.active, files, tool)
        self._routes = routes
        return routes


def _first_problem(validator: Validator, args: JsonValue) -> str | None:
    """The message of the first schema violation in ``args``, in a stable order."""
    errors = sorted(validator.iter_errors(args), key=str)
    return errors[0].message if errors else None


async def _secrets(
    store: PluginStore, owner_id: str, plugin_id: str, manifest: PluginManifest
) -> dict[str, SecretValue]:
    """The operator's values for the secrets ``manifest`` declares."""
    values = await store.secret_values(owner_id, plugin_id)
    return {name: value for name, value in values.items() if name in manifest.secrets}


async def _run(
    runtime: PluginRuntime,
    manifest: PluginManifest,
    files: Bundle,
    tool: PluginTool,
    args: Mapping[str, object],
    hosts: Iterable[str],
    secrets: Mapping[str, SecretValue],
) -> dict[str, object]:
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
