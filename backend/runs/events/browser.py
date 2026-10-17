"""The conversation's shared browser."""

from __future__ import annotations

from typing import Literal

from .base import Body


class BrowserOpened(Body):
    """A tool attached a browser to the conversation, so the client can offer its panel
    without re-reading the session. Not emitted when a call reuses the live one."""

    type: Literal["browser.opened"] = "browser.opened"
