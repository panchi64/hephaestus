"""Provider keys for eval runs, read from the environment or ``evals/voice/.env``.

The ``.env`` file is gitignored and shared by every suite. The key is only ever sent to
the dev instance, which stores it on a temporary endpoint that the run deletes when it ends.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_FILE = Path(__file__).parent.parent / "voice" / ".env"


def _from_file(path: Path, name: str) -> str | None:
    if not path.is_file():
        return None
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip().removeprefix("export ").strip() == name:
            return value.strip().strip("'\"") or None
    return None


def provider_key(name: str, env_file: Path = ENV_FILE) -> str | None:
    """The key named ``name``; the process environment wins over the file."""
    return os.environ.get(name) or _from_file(env_file, name)
