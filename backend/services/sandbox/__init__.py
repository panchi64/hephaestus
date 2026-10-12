"""Execution-sandbox capability — where the agent's code runs, and what it may reach.

Code runs in the platform VM (:mod:`services.vm`), reached through a per-conversation
:class:`SandboxSessionManager` that keeps the conversation's workspace in the VM while it
is in use and reaps the guest copy when idle — a reap that leaves the conversation's files
where they are. What a command may reach is decided by the VM's egress gateway, which
holds each workspace to its own allowlist.

``host`` is the other half — the OS-level confinement everything that runs *outside*
the VM is wrapped in, reading the same domain allowlist. Its callers are the host shell
and the approval-gated escape hatch for when the operator's own machine has to change,
which degrades and says so.
"""

from __future__ import annotations

from .base import (
    HostExecutionError,
    SandboxError,
    SandboxResult,
    SandboxSpec,
)
from .gitenv import git_config_pins
from .host import (
    HostConfinement,
    confine,
    denied_read_paths,
    host_scratch_dir,
    resolve_confinement,
    shutdown_confinement,
)
from .manager import SandboxSessionManager
from .preview import PreviewHandle
from .process import kill_tree, run_on_host, spawn_confined, stop_all_groups
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
    "HostConfinement",
    "HostExecutionError",
    "confine",
    "denied_read_paths",
    "git_config_pins",
    "host_scratch_dir",
    "kill_tree",
    "resolve_confinement",
    "run_on_host",
    "shutdown_confinement",
    "spawn_confined",
    "stop_all_groups",
]
