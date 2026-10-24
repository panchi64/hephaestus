"""Which approvals of a parked turn only the operator may give."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from core.config import Settings
from services.permissions.guards import (
    COMMAND_TOOLS,
    FILE_WRITE_TOOLS,
    GUARDED_TOOLS,
    changes_directory,
    guard,
    guard_of_approval,
)
from tools.guarded import shell_cwd
from tools.run_home import home_of

from .parking import ParkedTurn


class Decision(Protocol):
    """The operator's answer to one approval, as far as the guard reads it."""

    @property
    def tool_call_id(self) -> str: ...

    @property
    def approved(self) -> bool: ...

    @property
    def override_args(self) -> dict[str, object] | None: ...


async def guarded_approvals(
    decisions: Sequence[Decision],
    parked: ParkedTurn,
    settings: Settings,
) -> set[str]:
    """The approved calls only the operator may answer: those the park marked guarded, and
    those whose replacement arguments are guarded themselves."""
    deps_of_run = parked.compaction.deps if parked.compaction is not None else None
    home = await home_of(deps_of_run) if deps_of_run is not None else None
    cwd = shell_cwd(deps_of_run) if deps_of_run is not None else None
    chosen = {d.tool_call_id: d for d in decisions}
    found: set[str] = set()
    # Command tools run in order, so a `cd` moves where later calls start; after one,
    # relative paths can't be placed.
    cwd_known = True
    for call in parked.requests.approvals:
        decision = chosen.get(call.tool_call_id)
        name = call.tool_name
        override = decision.override_args if decision is not None and decision.approved else None
        if decision is not None and decision.approved:
            if guard_of_approval(parked.requests.metadata.get(call.tool_call_id)) is not None:
                found.add(call.tool_call_id)
            elif override is not None and name in GUARDED_TOOLS:
                # Unplaceable is not known harmless: a file write with no home, a command
                # with no deps, or one following a `cd`.
                unplaced = (name in FILE_WRITE_TOOLS and home is None) or (
                    name in COMMAND_TOOLS and (deps_of_run is None or not cwd_known)
                )
                reason = guard(
                    name,
                    override,
                    home=home,
                    settings=settings,
                    cwd=cwd,
                    cwd_known=cwd_known,
                    strict=True,
                )
                if unplaced or reason is not None:
                    found.add(call.tool_call_id)
        # Settled calls count too: they run.
        if cwd_known and changes_directory(name, override or call.args_as_dict()):
            cwd_known = False
    return found
