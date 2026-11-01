"""The smol connection every machine uses."""

import hashlib
from pathlib import Path

import smol

#: Pinned, so a ``SMOL_CLOUD_TOKEN`` in the environment can't move a machine to Smol Cloud.
LOCAL = smol.ConnectOptions(target="local")


def machine_prefix(data_dir: Path) -> str:
    """Machine names are global to this OS user, so each installation prefixes its own."""
    return "heph-" + hashlib.sha256(str(data_dir).encode()).hexdigest()[:8]
