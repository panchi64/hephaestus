"""What a bridge capability is: one method a plugin window can call on the platform.

A window's call reaches ``POST /plugins/bridge/{method}`` through the host app. The route
finds the capability, checks the window's plugin declared its ``permission``, asks the
capability whether the operator must confirm this call, and then runs its handler. A new
capability is one module in this package; neither the route nor the frontend names it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

from pydantic import JsonValue

from core.container import ServiceContainer
from core.exceptions import InvalidInputError

from ..windows import WindowSession

#: Starts a turn with ``prompt`` in ``conversation_id`` (a new chat when None) and returns
#: the conversation it went to.
type SendChat = Callable[[str, str | None], Awaitable[str]]


@dataclass(frozen=True)
class BridgeCall:
    window: WindowSession
    params: Mapping[str, JsonValue]
    services: ServiceContainer
    send_chat: SendChat

    def text(self, key: str, *, max_chars: int) -> str:
        value = self.params.get(key)
        if not isinstance(value, str) or not value:
            raise InvalidInputError(f"`{key}` is a non-empty string")
        if len(value) > max_chars:
            raise InvalidInputError(f"`{key}` is at most {max_chars} characters")
        return value

    def optional_text(self, key: str, *, max_chars: int) -> str | None:
        return None if self.params.get(key) is None else self.text(key, max_chars=max_chars)


@dataclass(frozen=True)
class Confirmation:
    """The operator is asked before the call runs."""

    #: Completes "<window title> wants to …".
    summary: str
    #: What "always allow" remembers, for this plugin's current version.
    grant: str


type Handler = Callable[[BridgeCall], Awaitable[JsonValue]]
type Asks = Callable[[BridgeCall], Awaitable[Confirmation | None]]


async def never(call: BridgeCall) -> Confirmation | None:
    del call
    return None


@dataclass(frozen=True)
class BridgeCapability:
    #: ``area.verb``, as the window calls it.
    method: str
    #: The ``permissions`` entry a plugin must declare to call it.
    permission: str
    handler: Handler
    #: Whether this call needs the operator's say-so first.
    asks: Asks = never
