"""The run's workspace, as Pydantic AI hands it to every tool as ``ctx.workspace``.

Files act on the run's home on the host; commands run in the conversation's machine,
which mounts the same folders (``services/home/workspace.py``). Workspace refs in the
message history are ignored: the machine is keyed by the conversation, not by a ref.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import override

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.workspaces import WorkspaceBackend, WorkspaceRef, WorkspaceUnavailableError

from services.egress import EgressPolicy
from services.home.table import RunHome
from services.home.workspace import HomeWorkspace, machine_spec
from services.offline import OfflineModeService
from services.projects.worktree import WorktreeBusyError
from services.vm.machines import ConversationMachines, MachineSpec

from .deps import RunDeps
from .home import resolve_run_home
from .workspace import unavailable


async def allowed_hosts(deps: RunDeps) -> tuple[str, ...] | None:
    """The hosts this run's commands may reach, or None for no network: offline mode, or
    nothing allowed. smol's allowlist takes a name and its subdomains, so ``*.x`` is ``x``."""
    offline = deps.caps.get_optional(OfflineModeService)
    if offline is not None and offline.state().effective_offline:
        return None
    policy = deps.caps.get_optional(EgressPolicy)
    if policy is None:
        return None
    domains = await policy.allowed_for(deps.scratch_key)
    hosts = sorted({domain.removeprefix("*.") for domain in domains})
    return tuple(hosts) or None


async def required_home(deps: RunDeps) -> RunHome:
    """The run's home, or raise ``WorkspaceUnavailableError`` saying why there is none."""
    try:
        resolved = await resolve_run_home(deps)
    except WorktreeBusyError as exc:
        raise WorkspaceUnavailableError(str(exc)) from exc
    if resolved is None:
        raise WorkspaceUnavailableError(unavailable(deps))
    return resolved


async def run_machine_spec(deps: RunDeps) -> MachineSpec:
    """What this run's machine mounts and reaches, as its commands get it."""
    return machine_spec(await required_home(deps), await allowed_hosts(deps))


def home_workspace(deps: RunDeps) -> HomeWorkspace:
    async def home() -> RunHome:
        return await required_home(deps)

    async def egress() -> tuple[str, ...] | None:
        return await allowed_hosts(deps)

    return HomeWorkspace(
        deps.workspace_key, home, egress, deps.caps.get_optional(ConversationMachines)
    )


@dataclass
class HomeWorkspaceCapability(AbstractCapability[RunDeps]):
    @override
    def get_workspace(
        self, ctx: RunContext[RunDeps], *, ref: WorkspaceRef | None
    ) -> WorkspaceBackend | None:
        return home_workspace(ctx.deps)
