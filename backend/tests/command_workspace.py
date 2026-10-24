"""A workspace that can run commands, so the shell category appears in a resolved catalog."""

from __future__ import annotations

import tempfile
from pathlib import Path

from pydantic_ai.workspaces import LocalWorkspaceBackend, Workspace


def command_workspace() -> Workspace:
    """The harness shell offers no tools to a run whose workspace cannot execute commands.
    Nothing here executes one: the catalog tests only read the tools' definitions."""
    return Workspace(LocalWorkspaceBackend(Path(tempfile.gettempdir())))
