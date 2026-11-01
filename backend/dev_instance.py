"""Dev instance entrypoint — ``uv run python dev_instance.py <command>``.

The sibling of ``dev.py``: that one starts the operator's Hephaestus, this one starts a
second, isolated Hephaestus for development — its own data directory, ports and
VMs, seeded and safe to break. ``devkit`` holds all of it; this file is only the
entrypoint, so that the command a session is told to run is a path in the repository
rather than a module invocation it has to get right.

Start with ``status``, which is also the answer to "what ports is this on".
"""

from __future__ import annotations

import sys

from devkit.cli import main

if __name__ == "__main__":
    sys.exit(main())
