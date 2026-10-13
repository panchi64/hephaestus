"""Bringing the instance up, and keeping it up.

``up`` **converges** rather than starts. It looks at what is already listening and fills
in only the gaps, so running it twice is safe, cheap, and the right thing to do when you
cannot remember whether a previous session left it running — which, for a session that
cannot see what came before it, is always. The alternative, a command that fails or
duplicates when something is already up, makes the first thing a reader must establish
"is it running?" rather than "what do I want to do?".

The backend is started through ``dev.py`` rather than uvicorn directly: it holds the
reload exclusions, and a dev instance is exactly where an agent writes files into the
data directory and would otherwise restart the server underneath its own work.
"""

from __future__ import annotations

import contextlib
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

from devkit import live, repo, surfaces
from devkit.instance import DevInstance
from devkit.processes import Supervisor, await_listening, listening

#: The name of the generated Claude Code launch configuration, and so the name a session
#: passes to `preview_start`. Fixed rather than per-slot: a session reads it from the
#: skill, and one name that always means "this worktree's instance" is what makes that
#: instruction correct in every worktree.
LAUNCH_CONFIG = "odysseus-dev"


class LaunchError(RuntimeError):
    """A service would not come up. The message says which, and where its log is."""


def backend_healthy(instance: DevInstance, *, timeout_s: float = 2.0) -> bool:
    """Whether the backend answers ``/health`` — not merely holds its port.

    The distinction matters on the failure this is here to catch: a backend that boots,
    binds, and then falls over during startup leaves the port taken. Probing the port
    alone would call that ready and hand a session a URL that answers nothing.
    """
    try:
        with urllib.request.urlopen(f"{instance.backend_url}/health", timeout=timeout_s) as reply:
            return reply.status == 200
    except urllib.error.URLError, OSError, ValueError:
        return False


def _await_health(instance: DevInstance, *, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if backend_healthy(instance):
            return True
        time.sleep(0.3)
    return False


def ensure_stub(instance: DevInstance, supervisor: Supervisor) -> str:
    """Start the scripted model unless one is already up, or a real endpoint is set."""
    if live.configured():
        return "using the endpoint in ODY_DEV_CHAT_* instead"
    if listening(instance.stub_port):
        return "already running"
    supervisor.start(
        "stub",
        [sys.executable, "-m", "devkit.stub.server", str(instance.stub_port)],
        cwd=repo.BACKEND,
        env=dict(instance.env()),
        log_dir=instance.logs_dir,
    )
    if not await_listening(instance.stub_port):
        raise LaunchError(f"the stub model did not start — see {instance.logs_dir / 'stub.log'}")
    return "started"


def ensure_backend(instance: DevInstance, supervisor: Supervisor) -> str:
    """Start the backend unless it is already answering."""
    if backend_healthy(instance):
        return "already running"
    if listening(instance.backend_port):
        raise LaunchError(
            f"port {instance.backend_port} is taken by something that is not this "
            f"instance's backend (it does not answer /health). Stop it, or remove "
            f"{instance.root / 'instance.json'} to be given a different slot."
        )
    supervisor.start(
        "backend",
        [sys.executable, "dev.py"],
        cwd=repo.BACKEND,
        env=dict(instance.env()),
        log_dir=instance.logs_dir,
    )
    if not _await_health(instance, timeout_s=90.0):
        raise LaunchError(
            f"the backend did not become healthy — see {instance.logs_dir / 'backend.log'}"
        )
    return "started"


def ensure_frontend_deps(instance: DevInstance) -> bool:
    """Install the frontend's dependencies if they are not there. Returns whether it ran.

    A fresh git worktree has no ``node_modules`` — that is the *ordinary* first run here,
    not an edge case — and without this the failure is ``vite: command not found`` behind
    a 127 exit code in a log file, which says nothing about what to do. Running the
    install is better than reporting it: there is exactly one correct response, and
    making a session read a log to discover it wastes the turn.
    """
    if (repo.FRONTEND / "node_modules").is_dir():
        return False
    instance.logs_dir.mkdir(parents=True, exist_ok=True)
    log = instance.logs_dir / "frontend.log"
    print("frontend dependencies are not installed — running `bun install` (first run only)")
    with log.open("a", buffering=1) as handle:
        handle.write("\n--- bun install ---\n")
        result = subprocess.run(
            ["bun", "install"], cwd=repo.FRONTEND, stdout=handle, stderr=subprocess.STDOUT
        )
    if result.returncode != 0:
        raise LaunchError(f"`bun install` failed — see {log}")
    return True


def ensure_frontend(instance: DevInstance, supervisor: Supervisor) -> str:
    """Start the vite dev server unless it is already up."""
    if listening(instance.frontend_port):
        return "already running"
    if shutil.which("bun") is None:
        raise LaunchError("`bun` is not on PATH — install it, or run the backend on its own")
    installed = ensure_frontend_deps(instance)
    supervisor.start(
        "frontend",
        # `--host 127.0.0.1`, because that is the address every URL this instance hands
        # out is spelled with. Vite's default is to bind whatever `localhost` resolves
        # to, which on a host that answers it with `::1` means the dev server comes up
        # perfectly and nothing at `http://127.0.0.1:<port>` ever answers — a frontend
        # that "did not start" with a log showing it ready.
        [
            "bun",
            "run",
            "dev",
            "--host",
            "127.0.0.1",
            "--port",
            str(instance.frontend_port),
            "--strictPort",
        ],
        cwd=repo.FRONTEND,
        env=dict(instance.frontend_env()),
        log_dir=instance.logs_dir,
    )
    if not await_listening(instance.frontend_port):
        raise LaunchError(f"the frontend did not start — see {instance.logs_dir / 'frontend.log'}")
    _warm_frontend(instance)
    return "started (installed dependencies)" if installed else "started"


def _warm_frontend(instance: DevInstance) -> None:
    """Fetch the page once so vite's dependency optimizer runs before anyone looks.

    On a first start the optimizer re-runs mid-request and the browser gets a 504 with a
    blank page, which reads as a broken app rather than as a build step. Best-effort: a
    warm-up that fails has cost a request, while a session that opens a blank page and
    concludes the change is broken has cost a turn.
    """
    with contextlib.suppress(urllib.error.URLError, OSError, ValueError):
        with urllib.request.urlopen(instance.frontend_url, timeout=60.0) as reply:
            reply.read()


def ensure_seeded(instance: DevInstance, *, force: bool = False) -> tuple[DevInstance, str]:
    """Seed the workspace if the fixture pack has changed since it last was.

    Data otherwise persists, which is the point: a scenario built up in one session is
    still there in the next. What must not persist is a workspace shaped by a *previous*
    fixture pack or a previous schema — that disagrees with the code silently, and gets
    debugged as a ghost. The version is derived from the fixture sources and the
    migration set, so both cases move it without anyone deciding to.
    """
    from devkit import seeding

    version = seeding.version()
    if instance.seeded == version and not force:
        return instance, f"already at {version}"
    summaries = seeding.seed(instance)
    seeded = instance.with_seeded(version)
    seeded.save()
    return seeded, f"seeded {version} — " + "; ".join(summaries)


def bring_up(
    instance: DevInstance, supervisor: Supervisor, *, reseed: bool = False
) -> tuple[DevInstance, dict[str, str]]:
    """Converge on a running instance, and report what each service did."""
    for directory in (instance.data_dir, instance.worktrees_dir, instance.logs_dir):
        directory.mkdir(parents=True, exist_ok=True)
    outcomes = {
        "stub": ensure_stub(instance, supervisor),
        "backend": ensure_backend(instance, supervisor),
    }
    # Before the frontend, so the first page a session opens is already populated.
    instance, outcomes["seed"] = ensure_seeded(instance, force=reseed)
    outcomes["frontend"] = ensure_frontend(instance, supervisor)
    # Written on every run rather than only the first: it carries the instance's ports,
    # and a stale one would point `preview_start` at another worktree's instance.
    surfaces.write_all(instance)
    return instance, outcomes


def wait_for_signal() -> None:
    """Block until interrupted, so the services this command started outlive it."""
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    stop.wait()
