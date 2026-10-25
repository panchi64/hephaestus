"""The `plugins` category: the model writing plugins, and the tools published ones add.

**What an agent writes is a draft.** It runs only through ``plugins_test``, in a
VM of its own, reaching only the hosts this conversation may already reach. The operator
reads the draft and publishes it; only then do its tools join this category,
each gated by the class its manifest declares and reaching only its declared hosts. A
plugin of windows alone is the exception: its pages run sandboxed with no network, so it
goes live as written.

A test that would send the operator's secrets asks first: a draft's code is whatever
the agent wrote, and a secret is the operator's authority at its host.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast, override

from pydantic import JsonValue
from pydantic_ai import AbstractToolset, FunctionToolset, ModelRetry, RunContext, Tool
from pydantic_ai.exceptions import ApprovalRequired
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import ToolsetTool, WrapperToolset
from pydantic_core import SchemaValidator, core_schema

from core.diagnostics import render_rejection, render_tool_error
from services.egress import EgressPolicy
from services.plugins import (
    PluginManifest,
    PluginRuntime,
    PluginSecretStore,
    PluginStore,
    PluginTool,
    PluginValidationError,
    argument_problem,
    plugin_secrets,
    run_plugin_tool,
)
from services.revisions import Bundle
from services.tool_sensitivity import SENSITIVITY_METADATA_KEY

from .deps import RunDeps
from .describe import registered_tools
from .plugin_windows import add_window_tools

#: Raised for approval only when a test would send the operator's secrets.
GATED_TOOLS: frozenset[str] = frozenset({"plugins_test"})

_UNAVAILABLE = "Plugins cannot be written in this deployment."
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
        """Writes a plugin, or revises it: a lasting capability the platform lacks. A plugin
        can add tools written in Python, each run in a VM of its own (such as a client for
        an API the operator uses), and small apps or games that open in a floating window
        over the app. For a one-off task, write and run the code directly instead.

        `plugin.yaml` sits beside the files:

            api: 1
            name: weather
            description: Forecasts.
            requirements: [httpx]          # pip lines
            egress: [api.example.com]      # the only hosts its tools can reach
            secrets: {API_KEY: api.example.com}
            tools:
              - name: forecast
                description: The forecast for a city.
                entry: weather:forecast    # module:function
                sensitivity: read          # or workspace_write, external_effect
                parameters: {type: object, properties: {city: {type: string}}}
            windows:
              - id: main
                title: Weather
                entry: ui/index.html
                size: {width: 360, height: 420}
            permissions: [storage, tools, chat.read, chat.post]

        A tool function takes its parameters as keyword arguments, may be async, and
        returns data that serializes to JSON. A secret's environment variable holds a
        placeholder that becomes the real value only in the URL or headers of an HTTPS
        request to its declared host, so the plugin never sees the secret itself.
        `agents/<name>.md` and `skills/<name>/SKILL.md` add agents and skills.

        A window is a page under `ui/` (html, js, css, json, svg, images, fonts, audio),
        sandboxed with no network at all: inline everything or ship it in `ui/`. It loads
        `<script src="__bridge.js"></script>` for `window.odysseus`, which reaches only what
        `permissions` lists: `storage.get/set/delete/list` (`storage`), `tools.call(name,
        args)` for this plugin's own tools (`tools`), `on("agent.status" |
        "agent.turn_finished", fn)` (`chat.read`), `chat.post(prompt)` (`chat.post`, which
        the operator confirms), plus `chat.open(prompt)` and `window.close()`.

        A plugin of only windows and permissions goes live at once (`status: enabled`);
        open it with `plugins_open_window`. Anything with tools, requirements, egress or
        secrets stays a draft (`status: draft`) until the operator publishes it. Run each
        tool with `plugins_test` before you tell the operator it is ready, and name any
        secret in `secrets_unset` that they still have to fill in.

        Returns the plugin's `name`, `status`, `tools`, `windows` and `secrets_unset`.

        Args:
            name: The plugin's name, in lowercase.
            files: Each path mapped to its full new text. Files you leave out are kept.
            remove: Paths to delete from the draft.
        """
        store = ctx.deps.caps.get_optional(PluginStore)
        secret_store = ctx.deps.caps.get_optional(PluginSecretStore)
        if store is None or secret_store is None:
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
            raise ModelRetry(render_rejection("the plugin", exc)) from None
        view = await store.publish_if_inert(ctx.deps.owner_id, view.id)
        manifest = view.current
        return {
            "ok": True,
            "name": view.name,
            "status": "draft" if view.draft is not None else "enabled",
            "tools": [tool.name for tool in manifest.tools],
            "windows": [window.id for window in manifest.windows],
            "secrets_unset": [
                s.name
                for s in await secret_store.secrets(ctx.deps.owner_id, view.id)
                if not s.is_set
            ],
        }

    @toolset.tool
    async def read(
        ctx: RunContext[RunDeps], name: str, version: Literal["current", "published"] = "current"
    ) -> dict[str, object]:
        """Returns a plugin's files, with whether it is `published` and whether it
        `has_draft`. Read it before revising a plugin, so your changes start from what is
        there.

        Args:
            name: The plugin's name.
            version: `current` for the draft when there is one, else the published
                version; `published` for the version its tools run now.
        """
        store = ctx.deps.caps.get_optional(PluginStore)
        if store is None:
            return {"ok": False, "error": _UNAVAILABLE}
        view = await store.get_by_name(ctx.deps.owner_id, name.strip().lower())
        if view is None:
            raise _no_such_plugin(name)
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
        """Runs one of a plugin's tools, from its draft when it has one, else from the
        published version, and returns its `result` or `error` and any `log` output.

        Use it to check a draft before telling the operator it works. The run reaches
        only the hosts the plugin declares that this conversation may also reach, so a
        host missing from either list fails here; ask for the conversation's side with
        `code_request_egress`. A test that would send the operator's secrets asks them
        first.

        Args:
            name: The plugin's name.
            tool: The tool's name within the plugin, as `plugin.yaml` declares it.
            args: The tool's arguments, as its `parameters` schema describes them.
        """
        store = ctx.deps.caps.get_optional(PluginStore)
        runtime = ctx.deps.caps.get_optional(PluginRuntime)
        secret_store = ctx.deps.caps.get_optional(PluginSecretStore)
        if store is None or runtime is None or secret_store is None:
            return {"ok": False, "error": _UNAVAILABLE}
        view = await store.get_by_name(ctx.deps.owner_id, name.strip().lower())
        if view is None:
            raise _no_such_plugin(name)
        manifest = view.current
        chosen = manifest.tool(tool)
        if chosen is None:
            raise ModelRetry(
                render_tool_error(
                    f"{view.name} has no tool {tool!r}",
                    hints=[f"its tools: {', '.join(t.name for t in manifest.tools)}"],
                )
            )
        secrets = await plugin_secrets(secret_store, ctx.deps.owner_id, view.id, manifest)
        if secrets and not ctx.tool_call_approved:
            raise ApprovalRequired()
        policy = ctx.deps.caps.get_optional(EgressPolicy)
        reachable: frozenset[str] = (
            await policy.allowed_for(ctx.deps.scratch_key) if policy else frozenset()
        )
        hosts = [host for host in manifest.egress if host in reachable]
        files = await store.files(ctx.deps.owner_id, view.id, "current")
        return await run_plugin_tool(runtime, manifest, files, chosen, args or {}, hosts, secrets)

    add_window_tools(toolset)
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
            raise ModelRetry(render_tool_error(f"`{name}` is no longer available"))
        problem = argument_problem(route.tool, tool_args)
        if problem is not None:
            raise ModelRetry(
                render_tool_error(
                    f"those arguments do not fit `{name}`",
                    cause=problem,
                    hints=["call it again with arguments that match its schema"],
                )
            )
        secret_store = ctx.deps.caps.get_optional(PluginSecretStore)
        if secret_store is None:
            raise ModelRetry(render_tool_error(f"`{name}` is no longer available"))
        secrets = await plugin_secrets(
            secret_store, ctx.deps.owner_id, route.plugin_id, route.manifest
        )
        return await run_plugin_tool(
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


def _no_such_plugin(name: str) -> ModelRetry:
    return ModelRetry(
        render_tool_error(
            f"there is no plugin called {name!r}",
            hints=["`platform_search` over the `plugins` source lists the published ones"],
        )
    )
