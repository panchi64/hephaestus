"""Starting the three services, waiting for them, and taking down only what we started.

"Only what we started" is the whole point of the module. ``up`` is meant to *converge* —
run it against a half-running instance and it fills in the gaps — which means it must be
able to tell a service it launched from one that was already there. Killing a backend
somebody else was using, on the way out of a command that was supposed to be idempotent,
is the kind of surprise that makes a tool untrustworthy after one occurrence.

Logs go to files rather than to this process's own output. Three interleaved streams are
unreadable, and a session that wants to know why the backend would not start is better
served by a path it can open than by a wall of text it has to have been watching for.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from devkit import ports

#: Where a running instance records the process groups it started, so a later, unrelated
#: command can stop them. Process *groups* rather than pids: both services spawn (the
#: backend's reloader runs the app in a child, `bun run dev` wraps vite), and signalling
#: only the handle we hold would leave the real server running with the port still taken.
PIDFILE = "pids.json"

#: How long to wait, per service, for a port to start accepting connections. Generous:
#: the frontend's first start compiles, and a cold Python import is not instant either.
STARTUP_TIMEOUT_S = 90.0

#: How long a terminated child gets to exit before it is killed outright.
_GRACE_S = 10.0


@dataclass(frozen=True, slots=True)
class Service:
    """One launched process and where its output went."""

    name: str
    process: subprocess.Popen
    log: Path


def listening(port: int) -> bool:
    """Whether something accepts connections on this port right now.

    **Every loopback address, because a server picks its own.** Uvicorn is told
    ``127.0.0.1``; vite, left to itself, binds the *name* ``localhost`` and takes whatever
    it resolves to first, which on a stock macOS is ``::1``. Probing v4 alone therefore
    never saw a frontend that had started in 200ms and was serving perfectly — ``up``
    waited out its ninety seconds and then tore the whole instance down, with a log showing
    a healthy server that had been sent SIGTERM for no stated reason. The frontend is now
    pinned to the address its URLs are spelled with (``launch.ensure_frontend``); this is
    the belt-and-braces half, so the next service to bind something else is merely noticed
    rather than declared dead.
    """
    for family, host in ports.LOOPBACKS:
        try:
            with socket.socket(family, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.25)
                if probe.connect_ex((host, port)) == 0:
                    return True
        except OSError:
            # No stack for that family on this host — not an answer either way.
            continue
    return False


def await_listening(port: int, *, timeout_s: float = STARTUP_TIMEOUT_S) -> bool:
    """Block until the port answers, or the deadline passes. ``False`` on timeout —
    the caller has a log path to point at, which is more use than an exception here."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if listening(port):
            return True
        time.sleep(0.2)
    return False


class Supervisor:
    """The processes this command started, and nothing else.

    Children are started in their own process group so that stopping one stops what it
    spawned. Both of ours spawn: the backend's reloader runs the app in a child of its
    own, and ``bun run dev`` is a wrapper around vite. Signalling only the process we
    hold a handle to would leave the actual server running and the port still taken —
    which reads, on the next ``up``, as an instance that is already healthy.
    """

    def __init__(self) -> None:
        self._services: list[Service] = []

    @property
    def services(self) -> list[Service]:
        return list(self._services)

    def start(
        self,
        name: str,
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        log_dir: Path,
    ) -> Service:
        """Launch one service, its output appended to ``<log_dir>/<name>.log``."""
        log_dir.mkdir(parents=True, exist_ok=True)
        log = log_dir / f"{name}.log"
        # Closed once Popen has dup'd it for the child: the parent's copy is pure leak,
        # and a caller that started services repeatedly would accumulate descriptors.
        with log.open("a", buffering=1) as handle:
            handle.write(f"\n--- {name} started {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                env=env,
                stdout=handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        service = Service(name=name, process=process, log=log)
        self._services.append(service)
        return service

    def record(self, root: Path) -> Path:
        """Add the process groups we started to what this instance has recorded.

        Recorded rather than discovered, because the alternative — finding whatever holds
        the port — would kill a process this instance never started, on a machine where
        the whole point is not to disturb what the operator is running.

        **Merged, never replaced.** ``up`` converges, so a run routinely starts only the
        services that were missing — or none at all. Writing only what *this* run started
        would drop the earlier run's groups, and an ``up`` that started nothing would
        clear the file outright, leaving three live services that ``stop`` can no longer
        find. Stale entries are dropped on the way past, so the file cannot grow forever.
        """
        path = root / PIDFILE
        groups = _read_groups(path)
        for name, group in list(groups.items()):
            if not _group_alive(group):
                del groups[name]
        for service in self._services:
            with contextlib.suppress(ProcessLookupError, OSError):
                groups[service.name] = os.getpgid(service.process.pid)
        path.write_text(json.dumps(groups, indent=2) + "\n")
        return path

    def forget(self, root: Path) -> None:
        """Drop only the services this run started from the record.

        The counterpart to the merge above, for the foreground path's teardown: a plain
        ``up`` that converged onto an already-running detached instance owns nothing, and
        deleting the file on its way out would strand what the detached run started.
        """
        path = root / PIDFILE
        groups = _read_groups(path)
        for service in self._services:
            groups.pop(service.name, None)
        if groups:
            path.write_text(json.dumps(groups, indent=2) + "\n")
        else:
            path.unlink(missing_ok=True)

    def stop_all(self) -> None:
        """Signal every child's process group, then wait, then insist."""
        for service in reversed(self._services):
            signal_group(_group_of(service), signal.SIGTERM)
        deadline = time.monotonic() + _GRACE_S
        for service in reversed(self._services):
            with contextlib.suppress(subprocess.TimeoutExpired):
                service.process.wait(timeout=max(0.1, deadline - time.monotonic()))
        for service in reversed(self._services):
            if service.process.poll() is None:
                signal_group(_group_of(service), signal.SIGKILL)
        self._services.clear()


def _group_of(service: Service) -> int | None:
    try:
        return os.getpgid(service.process.pid)
    except ProcessLookupError, OSError:
        return None


def _read_groups(path: Path) -> dict[str, int]:
    """The recorded ``{name: process group}``, or nothing if it cannot be read.

    An unreadable record is treated as absent rather than fatal: it is a convenience for
    stopping things, and refusing to start because of a corrupt one would be worse than
    the leftover it describes.
    """
    try:
        groups = json.loads(path.read_text())
    except OSError, ValueError:
        return {}
    return (
        {str(name): int(group) for name, group in groups.items()}
        if (isinstance(groups, dict))
        else {}
    )


def _group_alive(group: int) -> bool:
    """Whether any process is still in this group. Signal 0 checks without delivering."""
    try:
        os.killpg(group, 0)
    except ProcessLookupError, OSError:
        return False
    return True


def signal_group(group: int | None, sig: int) -> None:
    """Signal a process group, tolerating one that has already gone."""
    if group is None:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(group, sig)


def stop_recorded(root: Path) -> list[str]:
    """Stop the services a previous ``up`` recorded here. Returns what was signalled."""
    path = root / PIDFILE
    groups = _read_groups(path)
    if not groups:
        path.unlink(missing_ok=True)
        return []
    for group in groups.values():
        signal_group(group, signal.SIGTERM)
    time.sleep(1.0)
    for group in groups.values():
        signal_group(group, signal.SIGKILL)
    path.unlink(missing_ok=True)
    return list(groups)
