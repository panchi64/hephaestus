"""Execution-sandbox capability — where the agent's code runs, and what it may reach.

The default is the container path (``Sandbox`` + ``ContainerSandbox``), reached
through a per-conversation :class:`SandboxSessionManager` that keeps a container
warm for iterative work and reaps it when idle — a reap that takes the container
down and leaves the conversation's files where they are. What that container may reach is
decided at its network edge, not by its walls: an ``--internal`` network whose only
exit is the allowlisting proxy in ``sidecar``.

``host`` is the other half — the OS-level confinement everything that runs *outside*
a container is wrapped in, reading the same domain allowlist. Two callers land there:
the code-mode shell, which refuses without a fence, and the approval-gated escape
hatch for when the operator's own machine has to change, which degrades and says so.
"""

from __future__ import annotations

from .base import (
    HostExecutionError,
    Sandbox,
    SandboxError,
    SandboxFile,
    SandboxResult,
    SandboxSpec,
)
from .container import (
    ContainerSandbox,
    await_listening,
    await_log_marker,
    detached_run_argv,
    discover_runtime,
    ensure_image,
    force_remove_container,
    published_host_port,
    run_subprocess,
)
from .detect import detect_sandbox
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
    "Sandbox",
    "SandboxError",
    "SandboxFile",
    "SandboxResult",
    "SandboxSpec",
    "ContainerSandbox",
    "await_listening",
    "await_log_marker",
    "detached_run_argv",
    "discover_runtime",
    "ensure_image",
    "force_remove_container",
    "published_host_port",
    "run_subprocess",
    "detect_sandbox",
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
