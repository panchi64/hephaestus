"""Execution-sandbox capability — where the agent's code runs, and what it may reach.

Code runs in the platform VM (:mod:`services.vm`), reached through a per-conversation
:class:`SandboxSessionManager` that keeps the conversation's workspace in the VM while it
is in use and reaps the guest copy when idle — a reap that leaves the conversation's files
where they are. What a command may reach is decided by the VM's egress gateway, which
holds each workspace to its own allowlist.

``fence`` is the other half — the OS-level confinement the approval-gated escape hatch
(for when the operator's own machine has to change) is wrapped in, reading the same domain
allowlist; it degrades and says so. The host shell runs inside it too, with an open network.
``floor`` is the hardcoded deny list both are held to.
"""

from __future__ import annotations

from .base import (
    HostExecutionError,
    SandboxError,
    SandboxResult,
    SandboxSpec,
)
from .fence import (
    ConfinementState,
    HostConfinement,
    confine,
    confinement_state,
    denied_read_paths,
    fence_command,
    host_scratch_dir,
    resolve_confinement,
    shell_confinement,
    shutdown_confinement,
)
from .floor import is_protected_write
from .gitenv import git_config_pins
from .manager import SandboxSessionManager
from .preview import PreviewHandle
from .process import kill_tree, run_on_host, spawn_in_session, stop_all_groups
from .session import LiveWork, SandboxSession
from .staging import (
    STAGE_DIR,
    safe_name,
    stage_attachment,
    stage_unique,
    suffixed,
)

__all__ = [
    "SandboxError",
    "SandboxResult",
    "SandboxSpec",
    "LiveWork",
    "PreviewHandle",
    "SandboxSession",
    "SandboxSessionManager",
    "STAGE_DIR",
    "safe_name",
    "stage_attachment",
    "stage_unique",
    "suffixed",
    "ConfinementState",
    "HostConfinement",
    "HostExecutionError",
    "confine",
    "confinement_state",
    "denied_read_paths",
    "fence_command",
    "git_config_pins",
    "host_scratch_dir",
    "is_protected_write",
    "kill_tree",
    "resolve_confinement",
    "run_on_host",
    "shell_confinement",
    "shutdown_confinement",
    "spawn_in_session",
    "stop_all_groups",
]
