"""Managed SearXNG lifecycle against a fake services VM: the disabled/external/no-VM
short-circuits, the generated settings file, and the service bring-up and stop."""

from __future__ import annotations

import asyncio
import logging
import tempfile
from collections.abc import Callable, Coroutine, Sequence
from pathlib import Path
from typing import cast, override

import pytest

from services.searxng import ManagedSearxng
from services.vm import VMError
from services.vm.host import ServiceHost


class _ServiceVM(ServiceHost):
    """Records the calls ManagedSearxng makes; its forwarded port is a local HTTP stub."""

    def __init__(self, port: int) -> None:
        self.port: int = port
        self.calls: list[tuple[object, ...]] = []
        self.puts: list[tuple[str, str]] = []
        self.services: list[tuple[str, str, list[str]]] = []
        self.hooks: list[Callable[[], Coroutine[object, object, None]]] = []
        self.alive: bool = True
        self.log: str = ""
        self.start_error: VMError | None = None

    @override
    def on_ready(self, hook: Callable[[], Coroutine[object, object, None]]) -> None:
        self.hooks.append(hook)

    @override
    async def wait_ready(self, timeout_s: float) -> bool:
        self.calls.append(("wait_ready",))
        return True

    @override
    async def put_text(self, guest: str, text: str, *, mode: str = "0644") -> None:
        self.calls.append(("put_text", guest, text))
        self.puts.append((guest, text))

    @override
    async def start_service(self, name: str, user: str, argv: Sequence[str]) -> None:
        self.calls.append(("start_service", name, user, list(argv)))
        if self.start_error is not None:
            raise self.start_error
        self.services.append((name, user, list(argv)))

    @override
    async def service_alive(self, name: str) -> bool:
        return self.alive

    @override
    async def service_log(self, name: str, *, max_bytes: int) -> str:
        return self.log[-max_bytes:]

    @override
    async def forward(self, guest_port: int) -> int:
        self.calls.append(("forward", guest_port))
        return self.port

    @override
    async def stop_service(self, name: str) -> None:
        self.calls.append(("stop_service", name))

    def named(self, call: str) -> list[tuple[object, ...]]:
        return [c for c in self.calls if c[0] == call]


async def _http_stub(status: list[int] | int = 200) -> tuple[asyncio.Server, int]:
    """A local HTTP server answering with ``status``; pass a one-item list to change it later."""
    holder = status if isinstance(status, list) else [status]

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        _ = await reader.read(4096)
        writer.write(
            f"HTTP/1.1 {holder[0]} X\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode()
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    _host, port = cast(tuple[str, int], server.sockets[0].getsockname())
    return server, port


def _manager(
    *,
    enabled: bool = True,
    vm: _ServiceVM | None = None,
    data_dir: Path | None = None,
    startup_timeout_s: float = 2.0,
    external_base_url: str | None = None,
) -> ManagedSearxng:
    return ManagedSearxng(
        enabled=enabled,
        vm=vm,
        data_dir=data_dir or Path(tempfile.mkdtemp()),
        startup_timeout_s=startup_timeout_s,
        external_base_url=external_base_url,
        probe_window_s=0.1,
    )


async def _ready(m: ManagedSearxng) -> None:
    assert m.bring_up is not None
    await asyncio.wait_for(m.bring_up, timeout=5.0)


async def test_disabled_is_a_noop():
    m = _manager(enabled=False)
    await m.start()
    assert m.base_url is None
    await m.stop()


async def test_external_url_is_used_directly_and_nothing_is_managed():
    server, port = await _http_stub()
    vm = _ServiceVM(port)
    m = _manager(external_base_url="http://my.searx:8080/", vm=vm)
    await m.start()
    assert m.base_url == "http://my.searx:8080"  # trailing slash trimmed
    await m.stop()
    assert vm.calls == []  # no service started, none stopped
    server.close()


async def test_without_a_vm_the_base_url_stays_none():
    m = _manager(vm=None)
    await m.start()
    await _ready(m)
    assert m.base_url is None
    await m.stop()


async def test_bring_up_writes_settings_starts_the_service_and_forwards_its_port():
    server, port = await _http_stub()
    vm = _ServiceVM(port)
    m = _manager(vm=vm)
    async with server:
        await m.start()
        await _ready(m)

        assert m.base_url == f"http://127.0.0.1:{port}"
        (guest, text) = vm.puts[0]
        assert guest == "/etc/searxng/settings.yml"
        assert "proxies" not in text
        assert "port: 8888" in text
        assert "json" in text
        (name, _user, argv) = vm.services[0]
        assert name == "searxng" and "searx.webapp" in argv
        assert not any("PROXY" in part for part in argv)
        assert vm.named("forward") == [("forward", 8888)]
        # Settings land before the service starts, and the service before its port is forwarded.
        order = [c[0] for c in vm.calls]
        assert order.index("put_text") < order.index("start_service") < order.index("forward")
        await m.stop()


async def test_stop_clears_the_base_url_and_stops_the_service():
    server, port = await _http_stub()
    vm = _ServiceVM(port)
    m = _manager(vm=vm)
    async with server:
        await m.start()
        await _ready(m)
        assert m.base_url is not None

        await m.stop()

        assert m.base_url is None
        assert vm.named("stop_service")[-1] == ("stop_service", "searxng")


async def test_a_rebuilt_vm_gets_the_service_started_again():
    server, port = await _http_stub()
    vm = _ServiceVM(port)
    m = _manager(vm=vm)
    async with server:
        await m.start()
        await _ready(m)
        assert len(vm.named("start_service")) == 1

        await vm.hooks[0]()  # the VM came up again after a reset
        await _ready(m)

        assert len(vm.named("start_service")) == 2
        assert m.base_url == f"http://127.0.0.1:{port}"
        await m.stop()


async def _until(predicate: Callable[[], bool], timeout_s: float = 5.0) -> None:
    async with asyncio.timeout(timeout_s):
        while not predicate():
            await asyncio.sleep(0.02)


async def test_a_slow_server_is_reported_slow_and_still_waited_on(caplog: pytest.LogCaptureFixture):
    status = [503]
    server, port = await _http_stub(status)
    vm = _ServiceVM(port)
    vm.log = "loading engines..."
    m = _manager(vm=vm, startup_timeout_s=0.2)
    async with server:
        with caplog.at_level(logging.WARNING, logger="services.searxng"):
            await m.start()
            await _until(lambda: m.state == "slow")
        assert m.base_url is None
        assert m.bring_up is not None and not m.bring_up.done()
        assert "loading engines..." in caplog.text

        status[0] = 200
        await _ready(m)

        assert m.state == "ready"
        assert m.base_url == f"http://127.0.0.1:{port}"
        await m.stop()


async def test_a_service_that_exits_is_reported_crashed_with_its_output(
    caplog: pytest.LogCaptureFixture,
):
    server, port = await _http_stub(status=503)
    vm = _ServiceVM(port)
    vm.alive = False
    vm.log = "ModuleNotFoundError: No module named 'searx'"
    m = _manager(vm=vm)
    async with server:
        with caplog.at_level(logging.WARNING, logger="services.searxng"):
            await m.start()
            await _ready(m)
        assert m.state == "crashed"
        assert m.base_url is None
        assert "No module named 'searx'" in caplog.text
        await m.stop()


async def test_a_service_that_cannot_be_launched_is_reported_failed():
    vm = _ServiceVM(0)
    vm.start_error = VMError("bwrap: not found")
    m = _manager(vm=vm)
    await m.start()
    await _ready(m)
    assert m.state == "failed"
    assert m.base_url is None
    await m.stop()


async def test_state_is_off_when_disabled_or_stopped_and_ready_for_an_external_url():
    disabled = _manager(enabled=False)
    await disabled.start()
    assert disabled.state == "off"

    external = _manager(external_base_url="http://my.searx:8080")
    await external.start()
    assert external.state == "ready"
    await external.stop()
    assert external.state == "off"


def test_settings_enable_json_and_keep_a_stable_secret_key():
    data_dir = Path(tempfile.mkdtemp())
    m = _manager(data_dir=data_dir)

    text = m.settings()
    assert "json" in text  # the JSON API the agent queries is enabled
    assert "limiter: false" in text  # a single local operator, not a public instance

    key_first = (data_dir / "searxng" / "secret_key").read_text()
    _ = m.settings()  # a re-boot rewrites settings...
    key_second = (data_dir / "searxng" / "secret_key").read_text()
    assert key_first == key_second  # ...but reuses the persisted key
