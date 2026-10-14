"""Managed SearXNG — web search that runs itself, so the operator never sets it up.

The agent's `search` tool needs a search backend. Rather than make the operator stand
one up and register it, the backend runs its **own** SearXNG as a service in the platform
VM, reached on a forwarded loopback port, and the search service queries it
automatically. Its upstream requests leave through the VM's egress gateway, which allows
public addresses only.

Bring-up is **best-effort and non-fatal**: a VM that never comes up, or a server that
never binds, leaves :attr:`base_url` ``None`` and web search simply degrades (no host
fallback) — it never blocks app startup. The DB-backed provider registry
(``models/search``) stays as an optional override: an enabled provider there wins over
this managed instance.

When ``external_base_url`` is set the operator already runs SearXNG elsewhere — we use
that URL and manage nothing.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from pathlib import Path

from core import net
from core.tasks import STOP_TIMEOUT_S, cancel_and_wait
from services.vm import VMError
from services.vm.host import ServiceHost

logger = logging.getLogger(__name__)

_SERVICE = "searxng"
_USER = "searxng"
_PORT = 8888
_SETTINGS_PATH = "/etc/searxng/settings.yml"
_PYTHON = "/opt/hephaestus/searxng/bin/python"

# The secret key is generated once and reused across boots so session cookies stay
# stable; the settings file itself is rewritten each boot so template changes take effect.
_SETTINGS_TEMPLATE = """\
# Managed by Hephaestus — regenerated on each boot. Enables the JSON API the agent
# queries and disables the limiter (a single local operator, not a public instance).
use_default_settings: true
server:
  secret_key: "{secret_key}"
  bind_address: "0.0.0.0"
  port: {port}
  limiter: false
  image_proxy: false
search:
  formats:
    - html
    - json
outgoing:
  proxies:
    all://:
      - http://{proxy}
"""


class ManagedSearxng:
    """Owns the lifecycle of the backend's own SearXNG instance.

    :attr:`base_url` is ``None`` until the instance is ready (or forever, if the VM is
    unavailable); callers treat ``None`` as "managed search unavailable" and degrade.
    """

    def __init__(
        self,
        *,
        enabled: bool,
        vm: ServiceHost | None,
        data_dir: Path,
        startup_timeout_s: float,
        external_base_url: str | None = None,
    ) -> None:
        self._enabled: bool = enabled
        self._vm: ServiceHost | None = vm
        self._dir: Path = data_dir / "searxng"
        self._startup_timeout_s: float = startup_timeout_s
        self._external: str | None = external_base_url.rstrip("/") if external_base_url else None
        self._base_url: str | None = None
        self._task: asyncio.Task[None] | None = None
        if vm is not None:
            vm.on_ready(self._after_boot)

    @property
    def base_url(self) -> str | None:
        """The instance's URL once reachable, else ``None`` (degrade web search)."""
        return self._base_url

    @property
    def bring_up(self) -> asyncio.Task[None] | None:
        """The background bring-up task, or ``None`` before ``start`` launches one."""
        return self._task

    async def start(self) -> None:
        """Begin bring-up. Returns immediately — waiting for the VM and launching the
        service run in a background task so app startup is never blocked. A no-op when
        disabled."""
        if not self._enabled:
            logger.info("search: managed SearXNG disabled")
            return
        if self._external is not None:
            # The operator runs their own; nothing to manage.
            self._base_url = self._external
            logger.info("search: using external SearXNG at %s", self._external)
            return
        if self._task is not None:
            return
        logger.info("search: bringing up managed SearXNG in the background")
        self._task = asyncio.create_task(self._bring_up())

    async def stop(self) -> None:
        """Cancel an in-flight bring-up and stop the service. Resets to the pre-``start``
        state so a later ``start()`` brings it up afresh (the offline-mode monitor toggles
        this instance up and down)."""
        await cancel_and_wait(self._task, STOP_TIMEOUT_S)
        self._task = None
        self._base_url = None
        if self._vm is not None and self._external is None:
            try:
                await self._vm.stop_service(_SERVICE)
            except VMError:
                logger.info("search: could not stop managed SearXNG", exc_info=True)

    async def _after_boot(self) -> None:
        """Start again on a VM that was reset: what ran in the old one is gone."""
        if self._task is None or not self._task.done():
            return
        self._base_url = None
        self._task = asyncio.create_task(self._bring_up())

    async def _bring_up(self) -> None:
        vm = self._vm
        if vm is None:
            logger.info("search: no platform VM — managed web search unavailable")
            return
        _ = await vm.wait_ready(float("inf"))
        try:
            await vm.put_text(_SETTINGS_PATH, self.settings(vm.layout.platform_proxy))
            await vm.stop_service(_SERVICE)
            await vm.start_service(
                _SERVICE,
                _USER,
                ["env", f"SEARXNG_SETTINGS_PATH={_SETTINGS_PATH}", _PYTHON, "-m", "searx.webapp"],
            )
            host_port = await vm.forward(_PORT)
        except VMError as exc:
            logger.warning("search: managed SearXNG did not start: %s", exc)
            return
        if not await net.await_http_ready(
            f"http://127.0.0.1:{host_port}/", self._startup_timeout_s
        ):
            logger.warning("search: managed SearXNG did not answer in time")
            return
        self._base_url = f"http://127.0.0.1:{host_port}"
        logger.info("search: managed SearXNG ready at %s", self._base_url)

    def settings(self, proxy: str) -> str:
        """The settings file, with a secret key persisted across boots."""
        self._dir.mkdir(parents=True, exist_ok=True)
        key_file = self._dir / "secret_key"
        if key_file.exists():
            secret_key = key_file.read_text().strip()
        else:
            secret_key = secrets.token_hex(32)
            _ = key_file.write_text(secret_key)
            key_file.chmod(0o600)
        return _SETTINGS_TEMPLATE.format(secret_key=secret_key, port=_PORT, proxy=proxy)
