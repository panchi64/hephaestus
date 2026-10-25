"""``chat.post``: a window sending the agent a message, in a new chat or an existing one.

The message starts a turn with the operator's authority, so the operator confirms each one
(or always allows this plugin's current version to post).
"""

from __future__ import annotations

from pydantic import JsonValue

from .base import BridgeCall, BridgeCapability, Confirmation
from .registry import BRIDGE

PROMPT_MAX_CHARS = 4000
_SUMMARY_CHARS = 120


def _prompt(call: BridgeCall) -> str:
    return call.text("prompt", max_chars=PROMPT_MAX_CHARS)


def _conversation(call: BridgeCall) -> str | None:
    return call.optional_text("conversation_id", max_chars=64)


async def _asks(call: BridgeCall) -> Confirmation:
    prompt = " ".join(_prompt(call).split())
    shown = prompt if len(prompt) <= _SUMMARY_CHARS else prompt[: _SUMMARY_CHARS - 1] + "…"
    where = "a new chat" if _conversation(call) is None else "a chat"
    return Confirmation(summary=f'send "{shown}" to {where}', grant="chat.post")


async def _post(call: BridgeCall) -> JsonValue:
    conversation_id = await call.send_chat(_prompt(call), _conversation(call))
    return {"conversation_id": conversation_id}


BRIDGE.register(BridgeCapability("chat.post", "chat.post", _post, asks=_asks))
