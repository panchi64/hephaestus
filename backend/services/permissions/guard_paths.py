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
import pwd
import re
from dataclasses import dataclass, replace
from pathlib import Path

from services.home.paths import HOME
from services.home.table import RunHome
from services.permissions.shell_acts import EVERYTHING, WORKTREE
from services.permissions.shell_ast import OPAQUE
from services.sandbox.fence import host_scratch_dir
from services.sandbox.floor import fold, is_protected_write, is_under, write_floor

GITLINK_WHY = "a .git file can point git at a directory whose hooks run"

_SCRATCH = f"{HOME}/scratch"
_VM_PLACE = re.compile(rf"{re.escape(HOME)}/(projects|worktrees|shared)(/[^/]+)?")
_VM_GIT = re.compile(rf"{re.escape(HOME)}/(projects|worktrees)/[^/]+/\.git(/[^/]+)?")
_DATA = "the application's data folder is off limits"
_FIRMLINK = re.compile(r"^/System/Volumes/Data(?=/|$)")
_USER = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._-]*")
_YOUR_HOME = "your home folder"
_RUNTIME = "a path that is only known when it runs"


def _norm(path: str) -> str:
    collapsed = posixpath.normpath(re.sub(r"^/+", "/", path))
    return _FIRMLINK.sub("", collapsed) or "/"


def _user_home(name: str, *, vm: bool) -> str | None:
    if not _USER.fullmatch(name):
        return None
    if vm:
        return f"{HOME.rsplit('/', 1)[0]}/{name}"
    try:
        return pwd.getpwnam(name).pw_dir
    except KeyError:
        return None


def _resolve(text: str, *, tilde: str, cwd: str | None, vm: bool) -> str | None:
    """``text`` as an absolute normalised path, or None when it cannot be placed.

    ``~name`` is that user's home (``/home/name`` in the VM), ``~+`` the working directory;
    ``~-`` and a user that does not exist cannot be placed.
    """
    if not text or OPAQUE in text:
        return None
    if text == "~" or text.startswith("~/"):
        text = f"{tilde}/{text[2:].lstrip('/')}"
    elif text.startswith("~+"):
        rest = text[2:]
        if cwd is None or (rest and not rest.startswith("/")):
            return None
        text = f"{cwd}/{rest.lstrip('/')}"
    elif text.startswith("~"):
        name, _, rest = text[1:].partition("/")
        base = _user_home(name, vm=vm)
        if base is None:
            return None
        text = f"{base}/{rest.lstrip('/')}"
    elif vm and (text == "/work" or text.startswith("/work/")):
        text = _SCRATCH + text[len("/work") :]
    if text.startswith("/"):
        return _norm(text)
    return None if cwd is None else _norm(f"{cwd}/{text}")


def _glob_parent(path: str) -> str:
    """``path`` without trailing segments that match every entry, so ``/a/*`` names ``/a``.
    A pattern that matches only some entries (``*.log``) names entries, not the folder."""
    while posixpath.basename(path) in EVERYTHING:
        path = posixpath.dirname(path) or "/"
    return path


@dataclass(frozen=True)
class Places:
    """What a command's paths are measured against."""

    home: RunHome | None
    os_home: Path
    data_dir: Path
    vm_cwd: str | None
    host_cwd: str | None
    #: Whether the command runs on the host only, so the VM's reading never applies.
    host_only: bool = False

    @classmethod
    def at(cls, home: RunHome | None, os_home: Path, data_dir: Path, cwd: str | None) -> Places:
        vm_cwd = cwd or (home.default.path if home is not None else None)
        located = home.locate(vm_cwd) if home is not None and vm_cwd is not None else None
        host_cwd = str(located[0].root / located[1]) if located is not None else None
        return cls(home, os_home, data_dir, vm_cwd, host_cwd)

    @classmethod
    def for_host_command(cls, home: RunHome | None, os_home: Path, data_dir: Path) -> Places:
        """A command that starts in the host's scratch directory and never sees the VM."""
        return cls(home, os_home, data_dir, None, str(host_scratch_dir()), host_only=True)

    def after_cd(self, target: str | None) -> Places:
        """The places once the shell has changed directory to ``target`` (home if None)."""
        text = target if target is not None else "~"
        vm = _resolve(text, tilde=HOME, cwd=self.vm_cwd, vm=True)
        host = _resolve(text, tilde=str(self.os_home), cwd=self.host_cwd, vm=False)
        return replace(self, vm_cwd=vm, host_cwd=host)

    def walk(self, directories: tuple[str, ...]) -> Places:
        """The places after a sequence of ``cd`` words."""
        places = self
        for target in directories:
            places = places.after_cd(target)
        return places

    def _readings(self, text: str, *, host_first: bool) -> list[tuple[bool, str]]:
        """``(is_host, path)`` for each reading of ``text`` that can be placed."""
        host = _resolve(text, tilde=str(self.os_home), cwd=self.host_cwd, vm=False)
        found: list[tuple[bool, str]] = [] if host is None else [(True, host)]
        if not self.host_only:
            vm = _resolve(text, tilde=HOME, cwd=self.vm_cwd, vm=True)
            if vm is not None:
                found.append((False, vm))
        return found if host_first else sorted(found, key=lambda reading: reading[0])

    def _roots(self) -> list[tuple[Path, str]]:
        named = [
            (self.os_home, _YOUR_HOME),
            (self.data_dir, "the application's data folder"),
        ]
        if self.home is not None:
            named.extend(
                (m.root, "a project, worktree or workspace folder")
                for m in self.home.mounts
                if m.place.kind != "scratch"
            )
        return named

    def critical(self, text: str, *, host: bool = False) -> str | None:
        """What deleting ``text`` takes with it, when that is a path nobody deletes by
        accident: the filesystem root, the home folders, a project or worktree and its
        ``.git``, the data folder, or any folder holding one of them. ``host`` puts the
        host's reading first."""
        if text == WORKTREE:
            return "every untracked and ignored file in the working tree"
        readings = self._readings(text, host_first=host)
        if not readings:
            if text.startswith("~"):
                return _RUNTIME
            return self._unplaced(text)
        for is_host, path in readings:
            reason = self._host_critical(path) if is_host else self._vm_critical(path)
            if reason is not None:
                return reason
        expected = 1 if self.host_only else 2
        return _RUNTIME if text.startswith("~") and len(readings) < expected else None

    def _unplaced(self, text: str) -> str | None:
        """A relative path with no known working directory: the directory itself, or
        anything above it, is not a path to delete unasked."""
        stem = _glob_parent(posixpath.normpath(text))
        if stem in (".", "..") or stem.startswith("../"):
            return "the current directory or one above it"
        return None

    def _vm_critical(self, path: str) -> str | None:
        stem = _glob_parent(path)
        if stem == _SCRATCH or stem.startswith(_SCRATCH + "/"):
            return None
        if stem == "/":
            return "the filesystem root"
        if stem == HOME or HOME.startswith(stem + "/"):
            return "the agent's home folder or one above it"
        if _VM_GIT.fullmatch(stem):
            return "a repository's .git folder"
        if _VM_PLACE.fullmatch(stem):
            return "a project, worktree or shared folder"
        return None

    def _host_critical(self, path: str) -> str | None:
        stem = _glob_parent(path)
        if stem == "/":
            return "the filesystem root"
        for root, what in self._roots():
            for named in {root, root.resolve()}:
                for candidate in {Path(stem), Path(stem).resolve()}:
                    if is_under(named, candidate):
                        return (
                            what if fold(named) == fold(candidate) else f"a folder holding {what}"
                        )
        if self.home is not None:
            for mount in self.home.mounts:
                if mount.place.kind == "scratch":
                    continue
                git = mount.root / ".git"
                for candidate in {Path(stem), Path(stem).resolve()}:
                    for repository in {git, git.resolve()}:
                        if (
                            is_under(candidate, repository)
                            and len(fold(candidate)) <= len(fold(repository)) + 1
                        ):
                            return "a repository's .git folder"
        return None

    def is_dir(self, text: str, *, host: bool) -> bool:
        """Whether ``text`` names a directory, by how it is written or what is there."""
        if text in ("~", ".", "..") or text.endswith("/"):
            return True
        for is_host, path in self._readings(text, host_first=host):
            located = None if is_host or self.home is None else self.home.locate(path)
            if is_host:
                actual = Path(path)
            elif located is not None:
                actual = located[0].root / located[1]
            else:
                continue
            if actual.is_dir():
                return True
        return False

    def holds_protected(self, text: str, *, host: bool) -> bool:
        """Whether the directory ``text`` is, or holds, a path a command may not write."""
        if not host:
            return False
        for is_host, path in self._readings(text, host_first=True):
            if not is_host:
                continue
            if is_protected_write(Path(path) / "x", home=self.os_home) is not None:
                return True
            if any(is_under(Path(entry), Path(path)) for entry in write_floor(self.os_home)):
                return True
        return False

    def protected(self, text: str, *, host: bool, kind: str = "write") -> str | None:
        """Why a command may not write ``text`` unasked, or None. ``host`` is whether the
        command runs on the operator's machine rather than in the VM."""
        if OPAQUE in text or not text:
            return None
        if host or self.host_only:
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
            return GITLINK_WHY
        return why

    def _protected_host(self, path: Path, kind: str) -> str | None:
        why = is_protected_write(path, home=self.os_home)
        if why is not None:
            return why
        if kind == "write" and path.name.lower() == ".git":
            return GITLINK_WHY
        forms = {path, path.resolve()}
        scratch = [m for m in (self.home.mounts if self.home else ()) if m.place.kind == "scratch"]
        in_data = any(is_under(form, self.data_dir.resolve()) for form in forms)
        in_scratch = any(is_under(form, m.root.resolve()) for form in forms for m in scratch)
        return _DATA if in_data and not in_scratch else None
