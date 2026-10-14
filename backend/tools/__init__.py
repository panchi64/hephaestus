"""Tools — thin adapters over services/, exposed to the model as toolsets.

Which tools a run sees is *our* policy: a namespaced, enabled-gated toolset
stack (``toolsets.py``) keyed on the run's :class:`RunDeps`. Sensitive tools
pause for operator approval at execution, not filtered out. Logic never lives in
a tool — it delegates to a capability in ``services/``.

See docs/architecture/README.md (Pillar III, §2.2).
"""

from __future__ import annotations

from .builtin import builtin_toolset
from .deps import (
    NO_DISABLED_TOOLS,
    InstructionProvider,
    PromptContextProvider,
    PromptContextRequest,
    RunDeps,
    default_workspace_key,
)
from .harness_events import harness_events_capability
from .narration import NarrationCapability, strip_narration
from .tool_search import dormant_index_instructions, tool_search_capability
from .toolsets import CORE_GATED_TOOLS, build_agent_toolsets, core_categories

__all__ = [
    "NO_DISABLED_TOOLS",
    "InstructionProvider",
    "NarrationCapability",
    "PromptContextProvider",
    "PromptContextRequest",
    "RunDeps",
    "builtin_toolset",
    "build_agent_toolsets",
    "CORE_GATED_TOOLS",
    "core_categories",
    "default_workspace_key",
    "dormant_index_instructions",
    "harness_events_capability",
    "strip_narration",
    "tool_search_capability",
]
