"""Browser control — the agent drives a real page in the services VM, and the operator
watches and drives it through the app's live view.

:class:`VMBrowser` hands out web fetch's Chromium; :class:`BrowserSessionManager` gives
each conversation its own context on it, keeps the shared login profile, and reaps the
sessions nobody is using.
"""

from .live import ControlledBrowserSession, LiveBrowser, Profile
from .session import BrowserSessionManager
from .vm import VMBrowser

__all__ = [
    "BrowserSessionManager",
    "ControlledBrowserSession",
    "LiveBrowser",
    "Profile",
    "VMBrowser",
]
