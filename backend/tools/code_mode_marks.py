"""How a tool hook tells a call a ``run_code`` script made from a direct one.

Lives in ``tools/`` because the recovery layer needs it and ``tools/`` cannot import
``agent/``, where code mode is built.
"""

from __future__ import annotations

from pydantic_ai import RunContext

from .deps import RunDeps

#: The library's name for the tool, and the one name everything keyed on it uses.
RUN_CODE = "run_code"


def script_call_of(ctx: RunContext[RunDeps]) -> str | None:
    """The ``run_code`` call a tool hook's call was made from, or None for a direct call.

    The sandbox dispatches through its own ``ToolManager`` whose context is the ``run_code``
    call's; a direct call's manager is the run's, whose context names no tool.
    """
    manager = ctx.tool_manager
    outer = manager.ctx if manager is not None else None
    if outer is not None and outer.tool_name == RUN_CODE:
        return outer.tool_call_id
    return None
