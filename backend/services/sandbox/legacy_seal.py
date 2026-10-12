"""Reading back the archives written while a reap still sealed the workspace.

A workspace used to live as a plaintext directory while its sandbox was up and as
one vault-sealed tar the rest of the time. It no longer does — a reap wipes the
guest copy and leaves the directory alone — but an installation that ran the old
build still has archives on disk, and they hold conversations' real work.

So this module is the one-way door out of that format: **restore only, and delete the
archive once it has landed.** Nothing writes one any more. Every path here is dead the
moment no installation carries a `sandbox/sealed/` entry, and the whole file can go
then.
"""

from __future__ import annotations

import io
import logging
import tarfile
from pathlib import Path

from core.vault import Vault

from .base import SandboxError

logger = logging.getLogger(__name__)


def partial_marker(workspace: Path) -> Path:
    """The flag the old seal left beside a directory that is a *fragment* of its
    archive.

    Both directions between archive and plaintext were multi-step, and the process
    could die between the steps. A fragment holds *less* than the archive it came
    from, so the archive is what wins — which is still the right answer on the way
    out, and the only reason this marker outlives the code that wrote it.
    """
    return workspace.with_name(workspace.name + ".partial")


def retire_superseded_archive(archive: Path, workspace: Path) -> None:
    """Drop an archive that a materialised workspace has already outlived.

    Adoption unlinks the archive as its last step, so a process dying in the window
    between the extract finishing and that unlink leaves both on disk — and because the
    workspace is now complete, :func:`adopt_legacy_archive` is never reached again and
    the archive is never retired. This is the only other place that can notice, and it
    is safe precisely because nothing writes an archive any more: where both exist, the
    directory is the live copy by construction and the archive is the fragment of a
    migration that finished.
    """
    if workspace.is_dir() and archive.exists():
        archive.unlink(missing_ok=True)
        logger.info("sandbox: dropped %s's superseded sealed archive", workspace.name)


def adopt_legacy_archive(archive: Path, workspace: Path, vault: Vault) -> bool:
    """Unpack a pre-existing sealed archive into ``workspace`` and remove it.

    Returns whether anything was adopted. A missing archive is the ordinary case on
    any installation that never ran the sealing build, and answers ``False`` without
    touching the vault — so the common path costs one ``exists()``.

    The caller holds the workspace's disk lock; this is blocking IO and belongs off
    the event loop.
    """
    if not archive.exists():
        return False
    if not vault.is_unlocked:
        raise SandboxError(
            "cannot open this conversation's saved workspace: the vault is locked"
        )
    marker = partial_marker(workspace)
    try:
        raw = vault.decrypt_bytes(archive.read_bytes())
        # The marker goes down *before* the directory it describes, so a crash
        # mid-extract leaves a fragment that says so and is thrown away on the next
        # attempt — rather than a partial workspace indistinguishable from a whole one.
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
        workspace.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
            tar.extractall(workspace, filter="data")  # 'data' guards path traversal
    except SandboxError:
        raise
    except Exception as exc:  # noqa: BLE001 — a damaged archive is a legible failure
        raise SandboxError(f"could not open this conversation's saved workspace: {exc}") from exc
    marker.unlink(missing_ok=True)
    # Only now: the files are on disk, which is where they live from here on. Leaving
    # the archive would mean a second copy that nothing updates and the next restore
    # would silently prefer over the work done since.
    archive.unlink(missing_ok=True)
    logger.info("sandbox: adopted %s from its sealed archive", workspace.name)
    return True
