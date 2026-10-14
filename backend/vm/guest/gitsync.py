"""Keeps a project's git history in step across the VM boundary, from the guest's side.

The host sends the branch as a bundle; this builds a repository from it that the sandbox
user owns, with no remotes and no hooks. The host reads back the files git considers part
of the project, and the commits the agent added, as a bundle it checks before taking.

Standard library and the ``git`` binary only; it is copied into the guest at boot.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

_BRANCH_REF = "refs/heads/{}"


def _git(root: str, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-c", "safe.directory=*", "-c", "core.hooksPath=/dev/null", "-C", root, *args],
        check=check,
        capture_output=True,
    )


def _chown(root: str, uid: int) -> None:
    for dirpath, dirnames, filenames in os.walk(os.path.join(root, ".git")):
        os.lchown(dirpath, uid, uid)
        for name in [*dirnames, *filenames]:
            os.lchown(os.path.join(dirpath, name), uid, uid)


def init(root: str, bundle: str, source: str, branch: str, uid: str) -> int:
    """A fresh repository at ``root`` holding ``branch`` from ``bundle`` (``-`` for a
    repository with no commits yet), its index matching the branch and its working tree
    left to the files the host sends."""
    os.makedirs(root, exist_ok=True)
    _ = _git(root, "init", "-q", "-b", branch)
    _ = _git(root, "config", "user.name", "Hephaestus agent")
    _ = _git(root, "config", "user.email", "agent@hephaestus.invalid")
    if bundle == "-":
        _chown(root, int(uid))
        return 0
    return update(root, bundle, source, branch, uid)


def update(root: str, bundle: str, source: str, branch: str, uid: str) -> int:
    """Point ``branch`` at the bundle's ``source`` ref and reset the index to match,
    leaving the working tree alone."""
    ref = _BRANCH_REF.format(branch)
    _ = _git(root, "fetch", "-q", "--update-head-ok", bundle, f"+{source}:{ref}")
    _ = _git(root, "symbolic-ref", "HEAD", ref)
    _ = _git(root, "reset", "-q", "--mixed")
    _chown(root, int(uid))
    return 0


def files(root: str) -> int:
    """What git counts as the project: tracked files, and untracked ones it does not
    ignore, NUL-separated."""
    result = _git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    _ = sys.stdout.buffer.write(result.stdout)
    return 0


def head(root: str) -> int:
    ref = _git(root, "symbolic-ref", "-q", "HEAD", check=False).stdout.decode().strip()
    sha = _git(root, "rev-parse", "-q", "--verify", "HEAD", check=False).stdout.decode().strip()
    json.dump({"ref": ref, "sha": sha}, sys.stdout)
    return 0


def pack(root: str, base: str, branch: str, out: str) -> int:
    """The commits on ``branch`` after ``base``, as a bundle; exit 3 when ``base`` is not
    an ancestor, so the branch was rewritten rather than added to."""
    ref = _BRANCH_REF.format(branch)
    if _git(root, "merge-base", "--is-ancestor", base, ref, check=False).returncode != 0:
        return 3
    _ = _git(root, "bundle", "create", "-q", out, f"{base}..{ref}")
    return 0


if __name__ == "__main__":
    action, *rest = sys.argv[1:]
    handlers = {"init": init, "update": update, "files": files, "head": head, "pack": pack}
    sys.exit(handlers[action](*rest))
