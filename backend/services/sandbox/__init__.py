"""Execution-sandbox capability — where the agent's code runs, and what it may reach.

Code runs in the platform VM (:mod:`services.vm`), reached through a per-conversation
:class:`SandboxSessionManager` that keeps the conversation's workspace in the VM while it
is in use and reaps the guest copy when idle — a reap that leaves the conversation's files
where they are. What a command may reach is decided by the VM's egress gateway, which
holds each workspace to its own allowlist.

``fence`` is the other half — the OS-level confinement the approval-gated escape hatch
(for when the operator's own machine has to change) is wrapped in, reading the same domain
allowlist; it degrades and says so. The host shell is not fenced yet.
"""

from __future__ import annotations

from .base import (
    HostExecutionError,
    SandboxError,
    SandboxResult,
    SandboxSpec,
)
from .fence import (
    HostConfinement,
    confine,
    denied_read_paths,
    host_scratch_dir,
    resolve_confinement,
    shutdown_confinement,
)
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
    "spawn_in_session",
    "stop_all_groups",
]
