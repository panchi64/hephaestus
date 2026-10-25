"""Open plugin windows: what each one's page and bridge tokens stand for.

Opening a window mints two tokens. The **page** token is in the frame's URL, so the
plugin's own code can read it; it only serves the window's ``ui/`` files. The **bridge**
token stays with the host app, which sends it with each bridge call it makes for the
window; the plugin never sees it. Both hold a snapshot of the published version they
were minted for, and are dropped when the plugin changes or the vault locks.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Mapping
from dataclasses import dataclass

from .contributions.windows import UI_FOLDER, PluginWindow

#: Windows a plugin may have open at once; opening another drops its oldest.
MAX_PER_PLUGIN = 8


@dataclass(frozen=True)
class WindowSession:
    owner_id: str
    plugin_id: str
    plugin: str
    window: PluginWindow
    permissions: frozenset[str]
    #: The window's files, by path under ``ui/``.
    files: Mapping[str, bytes]
    #: Names the published version it was opened on, so what the operator allows one
    #: version is not carried over to code written later.
    digest: str
    page_token: str
    bridge_token: str

    @property
    def src(self) -> str:
        return f"/plugin-ui/{self.page_token}/{self.window.entry.removeprefix(UI_FOLDER)}"


def bundle_digest(files: Mapping[str, bytes]) -> str:
    digest = hashlib.sha256()
    for path, blob in sorted(files.items()):
        for part in (path.encode(), blob):
            digest.update(len(part).to_bytes(8, "big"))
            digest.update(part)
    return digest.hexdigest()


class WindowSessions:
    def __init__(self) -> None:
        self._by_page: dict[str, WindowSession] = {}
        self._by_bridge: dict[str, WindowSession] = {}

    def open(
        self,
        *,
        owner_id: str,
        plugin_id: str,
        plugin: str,
        window: PluginWindow,
        permissions: frozenset[str],
        files: Mapping[str, bytes],
    ) -> WindowSession:
        mine = [s for s in self._by_page.values() if s.plugin_id == plugin_id]
        for stale in mine[: max(0, len(mine) - MAX_PER_PLUGIN + 1)]:
            self._drop(stale)
        session = WindowSession(
            owner_id=owner_id,
            plugin_id=plugin_id,
            plugin=plugin,
            window=window,
            permissions=permissions,
            files={
                path.removeprefix(UI_FOLDER): blob
                for path, blob in files.items()
                if path.startswith(UI_FOLDER)
            },
            digest=bundle_digest(files),
            page_token=secrets.token_urlsafe(32),
            bridge_token=secrets.token_urlsafe(32),
        )
        self._by_page[session.page_token] = session
        self._by_bridge[session.bridge_token] = session
        return session

    def page(self, token: str) -> WindowSession | None:
        return self._by_page.get(token)

    def bridge(self, token: str) -> WindowSession | None:
        return self._by_bridge.get(token)

    def revoke(self, plugin_id: str) -> None:
        """Close every window of a plugin that was changed, switched off or deleted."""
        for session in [s for s in self._by_page.values() if s.plugin_id == plugin_id]:
            self._drop(session)

    def clear(self) -> None:
        self._by_page.clear()
        self._by_bridge.clear()

    def _drop(self, session: WindowSession) -> None:
        _ = self._by_page.pop(session.page_token, None)
        _ = self._by_bridge.pop(session.bridge_token, None)
