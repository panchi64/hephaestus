"""External tools — MCP servers + third-party connectors (`MCP-*`, `INTEG-*`, `AE-3.6`).

Every other category is a fixed catalog written here. This one is not: its tools are
whatever the operator's registered servers and configured connectors happen to expose, so
the category resolves itself *while a run is being composed* rather than at import.

**We do not hand-roll an MCP client.** A connected server is already an
``AbstractToolset`` (Pydantic AI's ``MCPToolset``), so it drops into the stack unchanged
and the model calls its tools exactly as it calls ours. A connector's actions are wrapped
in an ordinary ``FunctionToolset``. This module composes those, and adds the one thing the
library can't know: policy.

**External tools are sensitive by default (`AE-3.6`).** They are precisely the case the
`AE-3` sensitivity model can't enumerate — their effects are unknown to the system and may
be externally visible — so every call pauses for approval unless the operator has marked
*that specific tool* trusted. Trust is per tool, never per server: registering or enabling
a server says nothing about what any tool on it does. The gate is runtime-conditional, so
it follows ``tools/recall_gate.py``'s shape (raise ``ApprovalRequired`` unless
``ctx.tool_call_approved``) rather than the static ``requires_approval=True`` marking, and
it re-reads the decision on every call so revoking trust takes effect immediately.

The conversation-scoped auto-approval grants (`AE-3.7`) keep working unchanged on top of
this: the engine resolves them by the namespaced tool name before the call reaches here.
"""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

from pydantic_ai import AbstractToolset, CombinedToolset, FunctionToolset, RunContext
from pydantic_ai.exceptions import ApprovalRequired, ModelRetry
from pydantic_ai.toolsets import ToolsetTool

from core.exceptions import (
    DegradedCapabilityError,
    InvalidInputError,
    NotFoundError,
    SSRFError,
)
from core.untrusted import wrap_untrusted
from services.external_tools import ExternalTools, SourceKind
from services.integrations import IntegrationService, IntegrationView

from .deps import RunDeps

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Route:
    """Where a tool the model just called actually came from.

    The model sees ``external_{slug}_{tool}``; policy is keyed by the source and the
    tool's *own* name. This is the map between the two, built while composing, so the
    gate never has to parse a name back apart.
    """

    kind: SourceKind
    source_id: str
    tool_name: str
    source_label: str


def external_toolset() -> AbstractToolset[RunDeps]:
    """The external-tool category. Empty unless the operator has registered a server or
    configured a connector."""
    return ExternalCategoryToolset()


class ExternalCategoryToolset(AbstractToolset[RunDeps]):
    """The operator's external tools, resolved per run and gated per tool.

    Composition is deferred to the first ``get_tools`` because that is the first moment
    the capability and the owner are known — both arrive on ``RunContext.deps``, which is
    assembled per turn — and it happens once per turn rather than per step: connecting an
    MCP server is a process spawn or an HTTP handshake, not something to repeat per step.
    """

    def __init__(self) -> None:
        self._stack: AsyncExitStack | None = None
        self._inner: CombinedToolset[RunDeps] | None = None
        self._routes: dict[str, _Route] = {}
        # Tools the operator has switched off (`MCP-1`/`AE-3.3`) — resolved once while
        # composing, since a toggle mid-run isn't a case worth a per-step read.
        self._disabled: set[str] = set()

    @property
    def id(self) -> str | None:
        return "external"

    @property
    def label(self) -> str:
        return "the external tool category"

    async def __aenter__(self) -> ExternalCategoryToolset:
        self._stack = AsyncExitStack()
        await self._stack.__aenter__()
        return self

    async def __aexit__(self, *exc: Any) -> bool | None:
        stack, self._stack = self._stack, None
        self._inner = None
        self._routes = {}
        self._disabled = set()
        if stack is None:
            return None
        return await stack.__aexit__(*exc)

    async def get_tools(self, ctx: RunContext[RunDeps]) -> dict[str, ToolsetTool[RunDeps]]:
        inner = await self._compose(ctx)
        tools = await inner.get_tools(ctx)
        return {name: tool for name, tool in tools.items() if name not in self._disabled}

    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, Any],
        ctx: RunContext[RunDeps],
        tool: ToolsetTool[RunDeps],
    ) -> Any:
        route = self._routes.get(name)
        if route is None or self._inner is None:  # pragma: no cover - composed by get_tools
            raise ModelRetry(f"{name} is no longer available.")
        await self._gate(ctx, route)
        return await self._inner.call_tool(name, tool_args, ctx, tool)

    # --- the AE-3.6 trust gate -------------------------------------------------------

    async def _gate(self, ctx: RunContext[RunDeps], route: _Route) -> None:
        """Pause an external tool call for operator approval unless this specific tool is
        trusted — or the operator has just approved this very call.

        Read fresh on every call rather than from the composition snapshot, so revoking
        trust returns the tool to per-call approval immediately instead of at the next
        run. ``tool_call_approved`` is set on the re-invocation after an approval, so this
        raises once and then lets the call through.
        """
        if ctx.tool_call_approved:
            return
        handle = ctx.deps.caps.get_optional(ExternalTools)
        if handle is None:  # pragma: no cover - a route implies a live handle
            raise ApprovalRequired()
        decision = await handle.policy.get(
            ctx.deps.owner_id, route.kind, route.source_id, route.tool_name
        )
        if not decision.enabled:
            raise ModelRetry(
                f"{route.tool_name} on {route.source_label} is disabled by the operator."
            )
        if not decision.trusted:
            raise ApprovalRequired()

    # --- composition -----------------------------------------------------------------

    async def _compose(self, ctx: RunContext[RunDeps]) -> CombinedToolset[RunDeps]:
        if self._inner is not None:
            return self._inner
        handle = ctx.deps.caps.get_optional(ExternalTools)
        children: list[AbstractToolset[RunDeps]] = []
        routes: dict[str, _Route] = {}
        disabled: set[str] = set()
        owner = ctx.deps.owner_id

        if handle is not None:
            for view, client in await handle.mcp.live_toolsets(owner):
                if not await self._connect(client, view.name):
                    continue
                children.append(client.prefixed(view.slug))
                for tool in view.tools:
                    name = f"{view.slug}_{tool.name}"
                    routes[name] = _Route("mcp", view.id, tool.name, view.name)
                    if not tool.enabled:
                        disabled.add(name)

            connectors = await handle.integrations.live_connectors(owner)
            if connectors:
                toolset, connector_routes, connector_disabled = _connector_toolset(
                    handle.integrations, connectors
                )
                children.append(toolset)
                routes.update(connector_routes)
                disabled |= connector_disabled

        self._routes = routes
        self._disabled = disabled
        self._inner = CombinedToolset(children)
        return self._inner

    async def _connect(self, client: AbstractToolset[RunDeps], label: str) -> bool:
        """Open one server's connection for the life of the run, defensively.

        A server that has gone down since it was last connected must cost the operator a
        missing tool, never a failed turn — so each is entered on its own and a failure
        is logged and skipped.
        """
        if self._stack is None:
            self._stack = AsyncExitStack()
            await self._stack.__aenter__()
        try:
            await self._stack.enter_async_context(client)
        except Exception as exc:  # noqa: BLE001 - one dead server must not fail the run
            logger.info("external: MCP server %r is unreachable this run: %s", label, exc)
            return False
        return True


def _connector_toolset(
    service: IntegrationService, connectors: list[IntegrationView]
) -> tuple[FunctionToolset[RunDeps], dict[str, _Route], set[str]]:
    """One tool per connector action, so each is separately describable to the model and
    separately trustable by the operator (`AE-3.6`). A single generic "call this
    connector" tool would collapse read and write into one trust decision."""
    toolset: FunctionToolset[RunDeps] = FunctionToolset()
    routes: dict[str, _Route] = {}
    disabled: set[str] = set()
    for connector in connectors:
        for action in connector.actions:
            name = f"{connector.slug}_{action.name}"
            toolset.add_function(
                _connector_call(service, connector, action.name, action.takes_body),
                name=name,
                description=f"{connector.name}: {action.description}",
            )
            routes[name] = _Route("integration", connector.id, action.name, connector.name)
            if not action.enabled:
                disabled.add(name)
    return toolset, routes, disabled


def _connector_call(
    service: IntegrationService, connector: IntegrationView, action_name: str, takes_body: bool
):
    """Build the callable behind one connector action (`INTEG-2`).

    A thin adapter, as every tool here is: the service owns the request shape, the URL
    guards and the credential; this decides only retry-vs-degrade and marks what comes
    back as untrusted external content.
    """

    async def call(
        ctx: RunContext[RunDeps],
        params: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
    ) -> str:
        """Call the connector.

        Args:
            params: Values for the action's path placeholders; anything left over is sent
                as query parameters.
            body: JSON body, for actions that take one.
        """
        try:
            response = await service.call(
                ctx.deps.owner_id,
                connector.id,
                action_name,
                params=params,
                body=body if takes_body else None,
            )
        except (NotFoundError, InvalidInputError, DegradedCapabilityError, SSRFError) as exc:
            # The model can fix these by calling differently (a missing parameter, a
            # body where none is taken), so hand them back rather than failing the turn.
            raise ModelRetry(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - a transport failure is retryable too
            raise ModelRetry(f"{connector.name} could not be reached: {exc}") from exc
        # A connector's response is data from a third party, never instructions.
        return wrap_untrusted(f"HTTP {response.status}\n{response.body}", source=connector.name)

    return call
