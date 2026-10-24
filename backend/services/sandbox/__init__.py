"""Execution-sandbox capability — where the agent's code runs, and what it may reach.

Commands run in each conversation's VM (:mod:`services.vm`). This package holds the
other half: ``fence``, the OS-level confinement the approval-gated escape hatch (for when
the operator's own machine has to change) is wrapped in, which degrades and says so;
``floor``, the hardcoded deny list it is held to; and the staging of operator files into a
workspace.
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
    shutdown_confinement,
)
from .floor import is_protected_write
from .gitenv import git_config_pins
from .process import kill_tree, run_on_host, spawn_in_session, stop_all_groups
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
    "shutdown_confinement",
    "spawn_in_session",
    "stop_all_groups",
]
