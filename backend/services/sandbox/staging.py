"""Staging an operator's file into a conversation's sandbox workspace.

One implementation, two callers. A file the operator attaches to a message is written
into the conversation's sandbox ``/work`` so the agent can *read and compute on it*
rather than have its text poured into the model's context:

- **eagerly, at attach time** (``agent/attachments.py``) — the turn's marker names the
  path, and the model pages through the file itself;
- **on demand** (``tools/attachments.py``'s ``attachments_provision``) — the recovery
  path when a recycled session no longer holds the file, or when the agent wants an
  attachment from an older turn.

Both land on the same path for the same bytes, which is the point: the marker written on
the attach turn stays correct after a re-stage, so a replayed thread doesn't have to
learn a second filename.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Protocol

from .base import SandboxError

# Where staged attachments land inside the sandbox working directory.
STAGE_DIR = "attachments"
_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")
# Hard bound on the collision-suffix search below — sized generously above any
# realistic same-named-attachment count so a bug can't spin this forever.
_MAX_SUFFIX_ATTEMPTS = 1000


class _Stageable(Protocol):
    """The slice of :class:`~services.home.scratch.HostFiles` staging uses."""

    def read_file(self, relpath: str) -> bytes: ...

    def write_file(self, relpath: str, content: bytes) -> None: ...


def safe_name(filename: str, upload_id: str) -> str:
    """A filesystem-safe basename for the staged file. Strips any path components and
    unsafe characters; falls back to the upload id when nothing usable survives, so the
    file always lands at a predictable, escape-free path under the stage dir."""
    base = filename.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = _UNSAFE.sub("-", base).strip("-.")
    return cleaned or upload_id


def suffixed(relpath: str, n: int) -> str:
    """``relpath`` with a ``-{n}`` suffix inserted before its extension, for the next
    collision-safe candidate name (``attachments/data.csv`` → ``attachments/data-2.csv``)."""
    path = PurePosixPath(relpath)
    return str(path.with_name(f"{path.stem}-{n}{path.suffix}"))


def stage_unique(session: _Stageable, relpath: str, content: bytes) -> tuple[str, bool]:
    """Write ``content`` at ``relpath``, or the next available ``-2``/``-3``… suffixed
    name when that path is already taken by *different* bytes — two attachments sharing
    a sanitized basename (e.g. two files both named ``invoice.pdf``). The same bytes
    already at that path means the same attachment is simply being re-provisioned, so
    it is staged in place rather than renamed — re-provisioning stays idempotent.

    Returns ``(staged_relpath, renamed)``."""
    candidate = relpath
    for n in range(2, _MAX_SUFFIX_ATTEMPTS + 2):
        try:
            existing = session.read_file(candidate)
        except SandboxError:
            break  # nothing at this path — safe to use
        if existing == content:
            break  # the same file, already staged — reuse the same path
        candidate = suffixed(relpath, n)
    # If every suffix up to _MAX_SUFFIX_ATTEMPTS is genuinely taken (astronomically
    # unlikely), the loop exits without re-checking this final candidate — it is
    # written to unverified rather than exhaustively proven free.
    session.write_file(candidate, content)
    return candidate, candidate != relpath


def stage_attachment(
    session: _Stageable,
    *,
    filename: str,
    upload_id: str,
    content: bytes,
    prefix: str = "",
) -> tuple[str, bool]:
    """Stage one attachment's original bytes under ``attachments/`` in the run's
    workspace, returning ``(relpath, renamed)``. The single entry point both the eager
    attach-time path and the ``attachments_provision`` tool go through, so the two can
    never disagree about where a given file lives.

    ``prefix`` is the workspace's own scratch prefix — empty for a sandbox workspace,
    which is ours entirely, and ``.hephaestus/`` for a coding run's git worktree, where the
    operator's diff must not fill up with files they attached to a message.

    Raises :class:`SandboxError` when the workspace can't be written — the caller
    decides how to degrade."""
    return stage_unique(session, f"{prefix}{STAGE_DIR}/{safe_name(filename, upload_id)}", content)
