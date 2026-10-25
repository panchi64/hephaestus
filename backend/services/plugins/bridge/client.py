"""Permissions the host app enforces itself, with no backend method behind them.

``chat.read``: the agent's status and turn endings, forwarded to the window.
``chat.open``: opening a new chat with a prompt in its composer, sent by nobody.
"""

from __future__ import annotations

from .registry import BRIDGE

BRIDGE.declare_permission("chat.read")
BRIDGE.declare_permission("chat.open")
