"""Plugin windows the agent opens over the app."""

from __future__ import annotations

from typing import Literal

from .base import Body


class PluginWindowOpened(Body):
    """The agent asked for a published plugin's window to open. The client opens it only
    while the operator is viewing ``conversation_id``, and not on a replay."""

    type: Literal["plugin.window_open"] = "plugin.window_open"
    conversation_id: str
    plugin_id: str
    plugin: str
    window: str
    title: str
    width: int
    height: int
    min_width: int
    min_height: int
    resizable: bool
