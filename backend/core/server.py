"""The uvicorn entrypoint — a server that tells the app it is being stopped.

``uvicorn.run`` cannot be told to do anything at the moment a signal arrives, and that moment
is the whole problem: on Ctrl+C uvicorn stops accepting, then waits for every open
connection to finish *before* it runs the app's lifespan shutdown. This app's long-lived SSE
responses never finish on their own, and the lifespan is what would end them, so a plain
``uvicorn.run`` waited forever and a second Ctrl+C force-exited past the lifespan, abandoning
the write-behind queues and leaving the VM running.

:class:`HephaestusServer` closes that gap by raising :func:`core.shutdown.begin_shutdown` from
``handle_exit`` — the instant the signal lands, before uvicorn starts waiting — so every
stream ends and uvicorn can finish. ``timeout_graceful_shutdown`` stays as the backstop for
a connection that is not ours to end.

:func:`serve` is ``uvicorn.run`` re-spelled around that class. The re-spelling is needed for
reload mode too: uvicorn's reloader spawns the app in a child process and hands it
``server.run`` as the target, so the subclass travels with it (it is picklable by reference
and holds nothing but uvicorn's own state). Production runs the same thing —
``python -m core.server`` — rather than a bare ``uvicorn app:app``, which works but has only
the backstop timeout to end a stream.
"""

from __future__ import annotations

import logging
import os
import sys
from types import FrameType
from typing import override

import uvicorn
from uvicorn.supervisors import ChangeReload

from core.shutdown import begin_shutdown

# How long uvicorn waits for connections that did not end on the shutdown signal (a proxied
# preview mid-body, a slow client) before it cancels them. The streams we own end at once.
GRACEFUL_SHUTDOWN_TIMEOUT_S = 5

STARTUP_FAILURE = 3


class HephaestusServer(uvicorn.Server):
    """A uvicorn server that raises the app's shutdown signal as soon as it is stopped."""

    @override
    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        # Before ``super()``: raising the flag is what lets the connections close, and
        # nothing in the base handler can fail in a way that should skip it.
        begin_shutdown()
        super().handle_exit(sig, frame)


def serve(config: uvicorn.Config) -> None:
    """Run ``config`` on :class:`HephaestusServer` — ``uvicorn.run`` with our server class."""
    if config.reload or config.workers > 1:
        if not isinstance(config.app, str):
            logging.getLogger("uvicorn.error").warning(
                "The application must be an import string to enable 'reload' or 'workers'."
            )
            sys.exit(STARTUP_FAILURE)
    else:
        config.load_app()

    server = HephaestusServer(config=config)
    try:
        if config.should_reload:
            sock = config.bind_socket()
            ChangeReload(config, target=server.run, sockets=[sock]).run()
        else:
            server.run()
    except KeyboardInterrupt:
        pass
    finally:
        if config.uds and os.path.exists(config.uds):
            os.remove(config.uds)

    if not server.started and not config.should_reload:
        sys.exit(STARTUP_FAILURE)


def main() -> None:
    """Production entrypoint: ``python -m core.server`` (no reload)."""
    from core.config import get_settings

    settings = get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    serve(
        uvicorn.Config(
            "app:app",
            host=settings.host,
            port=settings.port,
            timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_TIMEOUT_S,
        )
    )


if __name__ == "__main__":
    main()
