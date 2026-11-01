"""``plugins_open_window``: opening a published plugin's window over the operator's app."""

from __future__ import annotations

from pydantic_ai import FunctionToolset, ModelRetry, RunContext

from core.diagnostics import render_tool_error
from runs import PluginWindowOpened
from services.plugins import PluginStore

from .deps import RunDeps
from .emit import RunEventEmitted


def add_window_tools(toolset: FunctionToolset[RunDeps]) -> None:
    @toolset.tool
    async def open_window(
        ctx: RunContext[RunDeps], name: str, window: str = "main"
    ) -> dict[str, object]:
        """Opens one of a live plugin's windows over the operator's app, beside this chat.
        It opens only if they are looking at this conversation; they can also open it any
        time from the app's window launcher.

        Args:
            name: The plugin's name.
            window: The window's `id` in `plugin.yaml`.
        """
        store = ctx.deps.caps.get_optional(PluginStore)
        if store is None or ctx.deps.conversation_id is None:
            return {"ok": False, "error": "Plugin windows cannot be opened from here."}
        view = await store.get_by_name(ctx.deps.owner_id, name.strip().lower())
        if view is None or view.active is None or not view.published:
            raise ModelRetry(
                render_tool_error(
                    f"{name!r} is not a live plugin",
                    hints=["a plugin with tools goes live once the operator publishes it"],
                )
            )
        chosen = view.active.window(window)
        if chosen is None:
            offered = ", ".join(w.id for w in view.active.windows) or "none"
            raise ModelRetry(
                render_tool_error(f"{view.name} has no window {window!r}", hints=[offered])
            )
        _ = await ctx.emit(
            RunEventEmitted(
                body=PluginWindowOpened(
                    conversation_id=ctx.deps.conversation_id,
                    plugin_id=view.id,
                    plugin=view.name,
                    window=chosen.id,
                    title=chosen.title,
                    width=chosen.width,
                    height=chosen.height,
                    min_width=chosen.min_width,
                    min_height=chosen.min_height,
                    resizable=chosen.resizable,
                )
            )
        )
        return {"ok": True, "opened": f"{view.name}/{chosen.id}"}
