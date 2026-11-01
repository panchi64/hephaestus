"""Each conversation's live preview: a dev server in its machine, published to the host.

One preview per conversation. Many dev servers listen on loopback only, which a published
port cannot reach, so ``portfwd.py`` relays the machine's network address to the server's
port. A machine that is reaped or restarted takes its preview with it; the token then
reads as stopped.
"""

from __future__ import annotations

import secrets
import shlex

import smol

from core import net

from .errors import VMError
from .machines import ConversationMachines, MachineSpec
from .preview import PreviewHandle
from .preview_tokens import PreviewTokens

#: The relay's port in the machine, which is the one published.
_RELAY_PORT = 47100
_RUN = "/tmp/hephaestus-preview"
_LOG_TAIL_BYTES = 4000
_EXEC_TIMEOUT_S = 30.0


class Previews:
    def __init__(self, machines: ConversationMachines, *, startup_timeout_s: float) -> None:
        self._machines: ConversationMachines = machines
        self._startup_timeout_s: float = startup_timeout_s
        self._tokens: PreviewTokens = PreviewTokens()
        self._live: dict[str, PreviewHandle] = {}
        machines.on_end(self._ended)

    async def start(
        self, key: str, spec: MachineSpec, *, working_dir: str, command: list[str], port: int
    ) -> PreviewHandle:
        """Run ``command`` in the conversation's machine, replacing its preview, and wait
        until it answers. Raises :class:`VMError` when it exits without serving."""
        async with self._machines.use(key, spec, working_dir=working_dir):
            machine = self._machines.machine(key)
            if machine is None:
                raise VMError("the conversation's machine is gone")
            await self._stop_server(machine)
            self._tokens.drop(key)
            _ = self._live.pop(key, None)
            launch = (
                f"mkdir -p {_RUN} && cd {shlex.quote(working_dir)} && "
                + f"(setsid {shlex.join(command)} > {_RUN}/server.log 2>&1 < /dev/null & "
                + f"echo $! > {_RUN}/server.pid) && "
                + f"(setsid python3 /opt/hephaestus/portfwd.py {_RELAY_PORT} {port} "
                + f"> {_RUN}/relay.log 2>&1 < /dev/null & echo $! > {_RUN}/relay.pid)"
            )
            _ = await _sh(machine, launch)
            host_port = await self._published(machine)
            serving = await net.await_http_ready(
                f"http://127.0.0.1:{host_port}/", self._startup_timeout_s
            )
            if not serving and not await _alive(machine):
                log = (await _sh(machine, f"tail -c {_LOG_TAIL_BYTES} {_RUN}/server.log")).strip()
                detail = "the preview server exited without serving a request"
                raise VMError(detail + (f"\n--- server logs ---\n{log}" if log else ""))
            handle = PreviewHandle(
                token=secrets.token_urlsafe(32),
                host_port=host_port,
                server_port=port,
                command=tuple(command),
            )
            self._tokens.index(handle.token, key)
            self._live[key] = handle
            return handle

    def resolve(self, token: str) -> PreviewHandle | None:
        """The running preview a proxy request names, or None. Viewing keeps it from the
        idle reaper."""
        key = self._tokens.owner(token)
        handle = self._live.get(key) if key is not None else None
        if key is None or handle is None or handle.token != token:
            return None
        self._machines.touch(key)
        return handle

    def status(self, token: str) -> str:
        """``running``, ``stopped`` (torn down without being closed) or ``unknown``."""
        key = self._tokens.owner(token)
        handle = self._live.get(key) if key is not None else None
        if handle is not None and handle.token == token:
            return "running"
        return "stopped" if self._tokens.was_stopped(token) else "unknown"

    async def stop(self, key: str) -> None:
        self._tokens.drop(key)
        if self._live.pop(key, None) is None:
            return
        machine = self._machines.machine(key)
        if machine is not None:
            await self._stop_server(machine)

    def _ended(self, key: str) -> None:
        handle = self._live.pop(key, None)
        if handle is not None:
            self._tokens.tombstone(handle.token)
            self._tokens.drop(key)

    async def _published(self, machine: smol.AsyncMachine) -> int:
        try:
            ports = await machine.ports()
            host = next((p.host for p in ports if p.guest == _RELAY_PORT), None)
            if host is None:
                host = (await machine.publish(_RELAY_PORT)).host
        except smol.SmolError as exc:
            raise VMError(f"could not publish the preview: {exc}") from exc
        if host is None:
            raise VMError("the preview was published without a host port")
        return host

    async def _stop_server(self, machine: smol.AsyncMachine) -> None:
        _ = await _sh(
            machine,
            f'for p in {_RUN}/server.pid {_RUN}/relay.pid; do [ -f "$p" ] && '
            + 'kill -TERM -- -"$(cat "$p")" 2>/dev/null; rm -f "$p"; done; true',
        )


async def _alive(machine: smol.AsyncMachine) -> bool:
    return (await _sh(machine, f'kill -0 "$(cat {_RUN}/server.pid)" 2>/dev/null && echo up')) != ""


async def _sh(machine: smol.AsyncMachine, command: str) -> str:
    try:
        options = smol.ExecOptions(timeout=_EXEC_TIMEOUT_S)
        result = await machine.exec(["sh", "-c", command], options)
    except smol.SmolError as exc:
        raise VMError(f"the conversation's machine did not answer: {exc}") from exc
    return result.stdout
