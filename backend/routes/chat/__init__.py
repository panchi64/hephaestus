"""Chat — create a Run that drives the agent over a prompt.

This is a *feature* package: it owns Run creation (the substrate doesn't), hands the
client a run id (and the conversation id), and the client then streams it from
``/runs/{id}/events``. A turn continues its conversation's history and persists its new
messages through the conversation store.

- ``schemas.py`` — the wire shapes.
- ``turn.py`` — ``resolve_turn_models``/``compose_turn``, the Request-agnostic halves of a
  turn, exported so a non-HTTP caller (the scheduler's agent-task executor) reuses them.
- ``submit.py`` — gathers a turn's resources from the ``Request`` and calls ``compose_turn``.
- ``send.py`` — send / regenerate / edit routes.
- ``settings.py`` — the operator's chat preferences.
"""

from __future__ import annotations

from fastapi import APIRouter

from routes.chat import send, settings
from routes.chat.submit import TurnScope, turn_scope
from routes.chat.turn import TurnModels, compose_turn, resolve_turn_models
from runs import ConversationBusyError

router = APIRouter()
router.include_router(send.router)
router.include_router(settings.router)

__all__ = [
    "ConversationBusyError",
    "TurnModels",
    "TurnScope",
    "compose_turn",
    "resolve_turn_models",
    "router",
    "turn_scope",
]
