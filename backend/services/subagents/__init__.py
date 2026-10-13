"""Delegation — the runtime that runs an agent on another agent's behalf.

What an agent *is* lives in ``services/agents``. This package is the launch seam
``tools/`` resolves (``launcher.py``, implemented in ``harness/manifests/_subagents.py``),
the standing brief every delegated agent gets (``briefing.py``), and the envelopes its
report and directions travel in (``report.py``).
"""

from __future__ import annotations

from services.subagents.launcher import (
    PARENT_ADDRESS,
    ROOT_HANDLE,
    LaunchedSubagent,
    Recipient,
    SubagentLauncher,
    SubagentParent,
    SubagentUnavailableError,
    SubagentView,
)

__all__ = [
    "PARENT_ADDRESS",
    "ROOT_HANDLE",
    "LaunchedSubagent",
    "Recipient",
    "SubagentLauncher",
    "SubagentParent",
    "SubagentUnavailableError",
    "SubagentView",
]
