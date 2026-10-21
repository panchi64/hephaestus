"""Which paths a guarded command must not delete or write.

A path means different things to the two places a command can run: in the VM ``~`` is the
agent's home (``/home/agent``) and a project is mounted under it, on the host ``~`` is the
operator's. Deleting is judged in both readings, because the VM's project mounts are the
operator's files coming back; writing is judged in the reading of the place the command
runs. A relative path is resolved against the shell's working directory, which a ``cd``
earlier in the same line moves.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, replace
from pathlib import Path

from services.home.paths import HOME
from services.home.table import RunHome
from services.permissions.shell_acts import WORKTREE
from services.permissions.shell_scan import OPAQUE
from services.sandbox.floor import fold, is_protected_write, is_under

_SCRATCH = f"{HOME}/scratch"
_VM_PLACE = re.compile(rf"{re.escape(HOME)}/(projects|worktrees|shared)(/[^/]+)?")
_GLOB = frozenset("*?[")
_GITLINK = "a .git file can point git at a directory whose hooks run"
_DATA = "the application's data folder is off limits"


def _norm(path: str) -> str:
    return posixpath.normpath(re.sub(r"^/+", "/", path))


def _resolve(text: str, *, tilde: str, cwd: str | None, vm: bool) -> str | None:
    """``text`` as an absolute normalised path, or None when it cannot be placed."""
    if not text or OPAQUE in text:
        return None
    if text == "~" or text.startswith("~/"):
        text = f"{tilde}/{text[2:].lstrip('/')}"
    elif vm and (text == "/work" or text.startswith("/work/")):
        text = _SCRATCH + text[len("/work") :]
    if text.startswith("/"):
        return _norm(text)
    return None if cwd is None else _norm(f"{cwd}/{text}")


def _glob_parent(path: str) -> str | None:
    """``path`` without trailing glob segments, so ``/a/*`` names ``/a``."""
    while any(char in _GLOB for char in posixpath.basename(path)):
        parent = posixpath.dirname(path)
        if not parent or parent == path:
            return None
        path = parent
    return path


@dataclass(frozen=True)
class Places:
    """What a command's paths are measured against."""

    home: RunHome | None
    os_home: Path
    data_dir: Path
    vm_cwd: str | None
    host_cwd: str | None

    @classmethod
    def at(cls, home: RunHome | None, os_home: Path, data_dir: Path, cwd: str | None) -> Places:
        vm_cwd = cwd or (home.default.path if home is not None else None)
        located = home.locate(vm_cwd) if home is not None and vm_cwd is not None else None
        host_cwd = str(located[0].root / located[1]) if located is not None else None
        return cls(home, os_home, data_dir, vm_cwd, host_cwd)

    def after_cd(self, target: str | None) -> Places:
        """The places once the shell has changed directory to ``target`` (home if None)."""
        text = target if target is not None else "~"
        vm = _resolve(text, tilde=HOME, cwd=self.vm_cwd, vm=True)
        host = _resolve(text, tilde=str(self.os_home), cwd=self.host_cwd, vm=False)
        return replace(self, vm_cwd=vm, host_cwd=host)

    def _roots(self) -> list[tuple[Path, str]]:
        named = [
            (self.os_home, "the home folder"),
            (self.data_dir, "the application's data folder"),
        ]
        if self.home is not None:
            named.extend(
                (m.root, "a project, worktree or workspace folder")
                for m in self.home.mounts
                if m.place.kind != "scratch"
            )
        return named

    def critical(self, text: str) -> str | None:
        """What deleting ``text`` takes with it, when that is a path nobody deletes by
        accident: the filesystem root, the home folders, a project or worktree, the data
        folder, or any folder holding one of them."""
        if text == WORKTREE:
            return "every untracked and ignored file in the working tree"
        vm = _resolve(text, tilde=HOME, cwd=self.vm_cwd, vm=True)
        host = _resolve(text, tilde=str(self.os_home), cwd=self.host_cwd, vm=False)
        if vm is None and host is None:
            return self._unplaced(text)
        return (self._vm_critical(vm) if vm else None) or (
            self._host_critical(host) if host else None
        )

    def _unplaced(self, text: str) -> str | None:
        """A relative path with no known working directory: the directory itself, or
        anything above it, is not a path to delete unasked."""
        stem = _glob_parent(posixpath.normpath(text))
        if stem is None or stem in (".", "..") or stem.startswith("../"):
            return "the current directory or one above it"
        return None

    def _vm_critical(self, path: str) -> str | None:
        stem = _glob_parent(path)
        if stem is None or stem == _SCRATCH or stem.startswith(_SCRATCH + "/"):
            return None
        if stem == "/":
            return "the filesystem root"
        if stem == HOME or HOME.startswith(stem + "/"):
            return "the agent's home folder or one above it"
        if _VM_PLACE.fullmatch(stem):
            return "a project, worktree or shared folder"
        return None

    def _host_critical(self, path: str) -> str | None:
        stem = _glob_parent(path)
        if stem is None:
            return None
        if stem == "/":
            return "the filesystem root"
        for root, what in self._roots():
            for named in {root, root.resolve()}:
                for candidate in {Path(stem), Path(stem).resolve()}:
                    if is_under(named, candidate):
                        return (
                            what if fold(named) == fold(candidate) else f"a folder holding {what}"
                        )
        return None

    def protected(self, text: str, *, host: bool, kind: str = "write") -> str | None:
        """Why a command may not write ``text`` unasked, or None. ``host`` is whether the
        command runs on the operator's machine rather than in the VM."""
        if OPAQUE in text or not text:
            return None
        if host:
            path = _resolve(text, tilde=str(self.os_home), cwd=self.host_cwd, vm=False)
            target = Path(path) if path is not None else Path("/") / text
            return self._protected_host(target, kind)
        virtual = _resolve(text, tilde=HOME, cwd=self.vm_cwd, vm=True)
        if virtual is None:
            return is_protected_write(Path("/") / text, home=self.os_home)
        located = self.home.locate(virtual) if self.home is not None else None
        target = located[0].root / located[1] if located is not None else Path(virtual)
        why = is_protected_write(target, home=self.os_home)
        if why is None and kind == "write" and target.name.lower() == ".git":
            return _GITLINK
        return why

    def _protected_host(self, path: Path, kind: str) -> str | None:
        why = is_protected_write(path, home=self.os_home)
        if why is not None:
            return why
        if kind == "write" and path.name.lower() == ".git":
            return _GITLINK
        forms = {path, path.resolve()}
        mounts = self.home.mounts if self.home is not None else ()
        in_data = any(is_under(form, self.data_dir.resolve()) for form in forms)
        in_mount = any(is_under(form, m.root.resolve()) for form in forms for m in mounts)
        return _DATA if in_data and not in_mount else None
