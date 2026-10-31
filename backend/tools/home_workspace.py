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
from services.permissions import AutoEnv, environment_clears
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


async def auto_env(deps: RunDeps, *, egress_requested: bool = False) -> AutoEnv:
    """The VM facts Auto's rule reads for this run, now. ``egress_requested`` is whether the
    batch being ruled on asks to open the network further, which would happen before its
    commands run."""
    machines = deps.caps.get_optional(ConversationMachines)
    policy = deps.caps.get_optional(EgressPolicy)
    try:
        home = await resolve_run_home(deps)
    except WorktreeBusyError:
        home = None
    granted = (
        await policy.granted_for(deps.scratch_key) if policy is not None else frozenset[str]()
    )
    return AutoEnv(
        vm_up=machines is not None and machines.platform.available,
        default_hosts_only=not egress_requested
        and (await allowed_hosts(deps) is None or not granted),
        writable_checkout=home is not None
        and any(m.writable and m.place.kind == "projects" for m in home.mounts),
    )


#: What the model is told when a call Auto cleared meets a VM that has changed since.
ENV_CHANGED = (
    "This command was not run: the conversation's VM changed (it gained network access or "
    "became unavailable) after Auto cleared it. Run it again to have it ruled on afresh."
)


async def env_still_clears(deps: RunDeps | None, tool: str) -> bool:
    """Whether the VM still bounds a call Auto cleared for running in it. False
    when the run's deps are gone, since then nothing can say."""
    if deps is None:
        return False
    cleared, _ = environment_clears(tool, await auto_env(deps))
    return cleared


async def still_bounded(ctx: RunContext[RunDeps], tool: str) -> bool:
    """False for a call Auto cleared for the VM when the VM has changed since."""
    call_id = ctx.tool_call_id
    if call_id is None or call_id not in ctx.deps.env_cleared:
        return True
    ctx.deps.env_cleared.discard(call_id)
    return await env_still_clears(ctx.deps, tool)


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
