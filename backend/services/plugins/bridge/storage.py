"""``storage.*``: the plugin's own key-value store."""

from __future__ import annotations

from pydantic import JsonValue

from ..storage import KEY_MAX_CHARS, PluginStorage
from .base import BridgeCall, BridgeCapability
from .registry import BRIDGE


def _store(call: BridgeCall) -> PluginStorage:
    return call.services.get(PluginStorage)


async def _get(call: BridgeCall) -> JsonValue:
    key = call.text("key", max_chars=KEY_MAX_CHARS)
    return await _store(call).get(call.window.owner_id, call.window.plugin_id, key)


async def _set(call: BridgeCall) -> JsonValue:
    key = call.text("key", max_chars=KEY_MAX_CHARS)
    await _store(call).set(
        call.window.owner_id, call.window.plugin_id, key, call.params.get("value")
    )
    return None


async def _delete(call: BridgeCall) -> JsonValue:
    key = call.text("key", max_chars=KEY_MAX_CHARS)
    await _store(call).delete(call.window.owner_id, call.window.plugin_id, key)
    return None


async def _list(call: BridgeCall) -> JsonValue:
    keys = await _store(call).keys(call.window.owner_id, call.window.plugin_id)
    return list[JsonValue](keys)


BRIDGE.register(
    BridgeCapability("storage.get", "storage", _get),
    BridgeCapability("storage.set", "storage", _set),
    BridgeCapability("storage.delete", "storage", _delete),
    BridgeCapability("storage.list", "storage", _list),
)
