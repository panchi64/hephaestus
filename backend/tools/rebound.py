"""Toolsets whose tools are fixed but whose *backing object* is not.

Two categories act on something a run resolves rather than the app: `files` on the run's
home, `browse` on a conversation's live browser. Categories are assembled once at app
startup and shared by every conversation, so neither can bake that in at construction.
`files` only needs it handed to it as ``ctx.workspace``; `browse` holds per-binding state,
so it is rebuilt per binding.

`AbstractToolset.for_run(ctx)` is not the hook for this, and it is worth saying why since
it looks like it: the library does call it once per run, but a harness toolset's `for_run`
rebuilds from **construction-time** values (`ShellToolset.for_run` passes its own
`_initial_cwd`). It isolates mutable per-run state — a `cd` that drifted the cwd, a
background process handle — between concurrent runs, which is real and useful, but it
cannot root two conversations at two different directories. That is exactly what is
needed here.

So the shape is: answer `get_tools` from a template bound to nothing real, because a
tool's *definition* — name, description, JSON schema — does not depend on where it will
act, which is what keeps the operator-facing catalog identical to the agent's real stack;
then re-resolve the tool from a correctly-bound instance inside `call_tool`. Pydantic AI
dispatches through `tool.call_func`, which is bound to the instance that produced it, so
delegating the call alone would still act on the template. That is the whole trick, and
:class:`ReboundToolset` is the one place it is implemented.

> A test that would pass while being wrong: "two runs don't share a cwd" is satisfied by
> `for_run` alone, with both runs rooted at the template's path. Assert the **roots
> differ and match their own conversations' workspaces**. The same trap applies to a
> browser session: "two conversations got different toolsets" passes while both drive the
> template's page.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Mapping
from typing import cast, override

from pydantic_ai import AbstractToolset, RunContext, Tool, ToolsetTool

from .deps import RunDeps
from .describe import registered_tools
from .harness_events import attributable

# Bound toolsets are cached per key because building one registers every tool and
# generates its schema — too much to repeat on each call. An evicted one is rebuilt on
# its next use, so nothing that must outlive a call may live only in one. The cap keeps a
# long-lived process from retaining an entry per conversation ever opened.
_MAX_BOUND = 64


class ReboundToolset(AbstractToolset[RunDeps]):
    """A harness toolset whose tools are defined once and dispatched through a per-run
    instance. Subclasses decide *what* to bind to by implementing :meth:`bind`."""

    def __init__(self, name: str, template: AbstractToolset[RunDeps]) -> None:
        self._id: str = name
        self._template: AbstractToolset[RunDeps] = template
        self._bound: OrderedDict[str, AbstractToolset[RunDeps]] = OrderedDict()
        # Resolved tools per bound toolset, keyed by object identity and evicted with it.
        self._tools: dict[int, dict[str, ToolsetTool[RunDeps]]] = {}

    @property
    @override
    def id(self) -> str:
        return self._id

    @property
    def tools(self) -> Mapping[str, Tool[RunDeps]]:
        """The static registry ``tools/catalog.py`` enumerates for the settings surface.
        Read from the template, so the catalog is the same set every run is offered."""
        return registered_tools(self._template)

    @override
    async def get_tools(self, ctx: RunContext[RunDeps]) -> dict[str, ToolsetTool[RunDeps]]:
        return await self._template.get_tools(ctx)

    async def bind(self, name: str, ctx: RunContext[RunDeps]) -> AbstractToolset[RunDeps] | str:
        """The toolset this call should act through — or a string to hand the model
        instead, when the capability is unavailable or the call is refused."""
        del name, ctx
        raise NotImplementedError

    async def resolve(
        self, name: str, ctx: RunContext[RunDeps]
    ) -> tuple[AbstractToolset[RunDeps], RunContext[RunDeps]] | str:
        """The bound toolset and the context to call it with, or a refusal string."""
        bound = await self.bind(name, ctx)
        return bound if isinstance(bound, str) else (bound, ctx)

    @override
    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, object],
        ctx: RunContext[RunDeps],
        tool: ToolsetTool[RunDeps],
    ) -> object:
        resolved = await self.resolve(name, ctx)
        if isinstance(resolved, str):
            return resolved
        bound, ctx = resolved
        # Re-resolve the tool against the bound toolset: the object handed in carries the
        # template's function, which would act on the template's binding. Resolved from
        # the per-key cache rather than by rebuilding the whole dict — the set is fixed
        # for a given binding, and this runs on every single call.
        tools = await self._tools_for(bound, ctx)
        # Named as the catalog names it, so a capability event the harness toolset emits can
        # be attributed: a prefixed toolset hands us the unprefixed `tool_name`, which is not
        # the key the library looks the owner up under (`tools/harness_events.py`).
        inward = attributable(ctx, f"{self.id}_{name}")
        return cast(object, await bound.call_tool(name, tool_args, inward, tools[name]))

    async def _tools_for(
        self, bound: AbstractToolset[RunDeps], ctx: RunContext[RunDeps]
    ) -> dict[str, ToolsetTool[RunDeps]]:
        """The bound toolset's resolved tools, built once per binding.

        Keyed by the toolset object's identity so it is evicted with it — a stale entry
        for a reaped workspace would otherwise hand back tools bound to something that no
        longer exists.
        """
        cached = self._tools.get(id(bound))
        if cached is None:
            cached = await bound.get_tools(ctx)
            self._tools[id(bound)] = cached
        return cached

    def cached(
        self, key: str, build: Callable[[], AbstractToolset[RunDeps]]
    ) -> AbstractToolset[RunDeps]:
        """The bound toolset for ``key``, built on first use and kept most-recently-used."""
        bound = self._bound.pop(key, None)
        if bound is None:
            bound = build()
            if len(self._bound) >= _MAX_BOUND:
                _, evicted = self._bound.popitem(last=False)
                _ = self._tools.pop(id(evicted), None)
        self._bound[key] = bound
        return bound
