"""End to end: one SIGINT stops a real server that has an open notification stream.

This is the failure the shutdown signal exists for. uvicorn waits for open connections
before it runs the app's lifespan, and the notification feed never ends on its own — so
without ``core.server`` raising the signal the process sat there until a second Ctrl+C.
It runs a real subprocess because that interaction (signal -> handler -> streams end ->
lifespan) is exactly what an in-process client cannot reproduce.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import cast

import httpx
import pytest

BACKEND = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        _host, port = cast(tuple[str, int], sock.getsockname())
        return port


def _env(tmp: Path, port: int) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("HEPHAESTUS_")}
    env.update(
        PYTHONPATH=str(BACKEND),
        HEPHAESTUS_HOST="127.0.0.1",
        HEPHAESTUS_PORT=str(port),
        # Everything under a throwaway directory — never the operator's data/.
        HEPHAESTUS_DATA_DIR=str(tmp / "data"),
        HEPHAESTUS_DB_URL=f"sqlite:///{tmp / 'app.db'}",
        HEPHAESTUS_WORKTREES_DIR=str(tmp / "worktrees"),
        HEPHAESTUS_AUTH_ENABLED="false",
        HEPHAESTUS_UNLOCK_PASSPHRASE="test-passphrase",
        HEPHAESTUS_SEARXNG_ENABLED="false",
        HEPHAESTUS_WEB_FETCH_ENABLED="false",
        HEPHAESTUS_SANDBOX_ENABLED="false",
        # Off too: on a machine with QEMU the VM's first start builds an image, and this
        # test is about stopping the server, not about cancelling a build.
        HEPHAESTUS_VM_ENABLED="false",
        HEPHAESTUS_OFFLINE_CHECK_ENABLED="false",
    )
    return env


def _wait_up(port: int, proc: subprocess.Popen[bytes], timeout_s: float = 60.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"server exited early with {proc.returncode}")
        try:
            if httpx.get(f"http://127.0.0.1:{port}/health", timeout=1.0).status_code == 200:
                return
        except httpx.HTTPError:
            time.sleep(0.2)
    raise AssertionError("server did not come up")


def test_sigint_stops_a_server_with_an_open_notification_stream(tmp_path: Path):
    port = _free_port()
    # cwd is the throwaway dir so a developer's backend/.env is never read.
    proc = subprocess.Popen(
        [sys.executable, "-m", "core.server"],
        cwd=tmp_path,
        env=_env(tmp_path, port),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_up(port, proc)
        with httpx.Client(timeout=httpx.Timeout(10.0, read=None)) as client:
            with client.stream("GET", f"http://127.0.0.1:{port}/notifications/stream") as resp:
                assert resp.status_code == 200
                proc.send_signal(signal.SIGINT)
                started = time.monotonic()
                # The stream must close from the server side, which is what lets it exit.
                for _ in resp.iter_raw():
                    pass
                assert proc.wait(timeout=8.0) == 0
                assert time.monotonic() - started < 8.0
    finally:
        if proc.poll() is None:
            proc.kill()
            _ = proc.wait()
