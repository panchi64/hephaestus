"""Development server entrypoint — ``uv run python dev.py``.

Plain ``uvicorn app:app --reload`` is a trap in this repo: its reloader watches runtime
state as if it were source, and restarts the server in the middle of the first serve of a
model. ``core.devserver`` explains the failure and holds the exclusions; this file is just
the entrypoint that applies them.

It starts uvicorn through ``core.server`` rather than ``uvicorn.run`` so that Ctrl+C ends the
app's open streams instead of waiting on them (see that module); ``timeout_graceful_shutdown``
is the backstop for a connection that is not ours to end.

Reload is a development affordance only. In production run ``python -m core.server``.
"""

from __future__ import annotations

from pathlib import Path

import uvicorn

from core.config import get_settings
from core.devserver import reload_excludes
from core.server import GRACEFUL_SHUTDOWN_TIMEOUT_S, serve

BACKEND_DIR = Path(__file__).resolve().parent


def main() -> None:
    settings = get_settings()
    # An exclusion only becomes a directory exclusion if the directory exists when the
    # reloader starts; a missing path silently falls back to the useless glob form. The
    # app's own startup creates this anyway — doing it first just makes the guard real on
    # a first-ever run, which is exactly the run that installs an engine.
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    serve(
        uvicorn.Config(
            "app:app",
            host=settings.host,
            port=settings.port,
            reload=True,
            reload_dirs=[str(BACKEND_DIR)],
            reload_excludes=reload_excludes(settings.data_dir, BACKEND_DIR),
            timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_TIMEOUT_S,
        )
    )


if __name__ == "__main__":
    main()
