"""Pillar III — the agent engine (Pydantic AI's Agent plus our meta-loop).

Within a turn we drive ``agent.iter()`` and translate its node stream into our
event protocol (Pillar II). Around it we will own the meta-loop: the deliverable
deliverable verifier, the no-progress loop-breaker, history processors for
context reduction, and model fallback. RunDeps/RunContext carry
per-run policy and capability handles to the tools.

See docs/architecture/README.md (Pillar III).
"""

from __future__ import annotations

from tools import RunDeps

from .engine import build_chat_orchestrator, build_resume_orchestrator
from .parking import AwaitingKind, ParkedTurn, awaiting_kind
from .translate import stream_agent_run

__all__ = [
    "RunDeps",
    "AwaitingKind",
    "ParkedTurn",
    "awaiting_kind",
    "build_chat_orchestrator",
    "build_resume_orchestrator",
    "stream_agent_run",
]
