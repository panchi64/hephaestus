"""A project folder's guest copy: the working set a project shell runs against.

A project chat works in the operator's own checkout (or its thread's worktree), and its
commands run in the platform VM, not on the host. What crosses is what git counts as the
project — tracked files, and untracked ones the project does not ignore — plus the branch
as a git bundle, so `git status`, `git diff` and `git commit` work in the guest. Ignored
paths (a `node_modules`, a `.venv`, build output) never cross in either direction: the
guest builds its own, and the host's are never overwritten with Linux builds.

Coming back, what changed is checked like any archive out of the VM (inside the tree, no
symlink escapes), and additionally may not touch `.git/` or link outside the tree.
Commits the agent made come back only as a fast-forward of the branch that was sent,
verified by git as they are fetched; a rewritten branch, or work on another branch, stays
in the guest and is reported.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

from core.periodic import PeriodicTask
from services.sandbox.gitenv import git_config_pins

from .driver import VMError
from .platform import SANDBOX_UID, PlatformVM
from .sync import Manifest, diff, scan, scan_names

logger = logging.getLogger(__name__)

#: The branch name the guest uses when the host checkout has none (a detached HEAD).
_DETACHED = "detached"
_GIT_TIMEOUT_S = 300.0
_MAX_BUNDLE_BYTES = 2 * 1024**3


@dataclass
class BringOut:
    """What a move out of the guest did, worded for the agent where it matters."""

    files: int = 0
    commits_landed: bool = False
    notes: list[str] = field(default_factory=list)


class ProjectWorkspace:
    def __init__(self, vm: PlatformVM, key: str, root: Path) -> None:
        self._vm = vm
        self.key = key
        self.root = root
        self._host: Manifest = {}
        self._guest: Manifest = {}
        self._generation: int | None = None
        self._branch: str | None = None
        #: The commit the guest's branch was last known to match the host's at.
        self._synced_sha: str | None = None
        #: Whether the folder is the top of a git repository; a plain folder crosses whole.
        self._git = False

    @property
    def is_in(self) -> bool:
        return self._generation == self._vm.generation and self._vm.available

    @property
    def guest_root(self) -> str:
        return self._vm.layout.work(self.key)

    async def bring_in(self) -> None:
        fresh = not self.is_in
        if fresh:
            self._host, self._guest = {}, {}
            self._git = await self._is_repository_top()
            await self._vm.wipe(self.key)
        if self._git:
            branch, sha = await self._host_head()
            if fresh or branch != self._branch or sha != self._synced_sha:
                await self._send_history(branch, sha, fresh=fresh)
        host_now = await self._host_manifest()
        changes = diff(host_now, self._host)
        if changes or fresh:
            await self._vm.push(self.key, self.root, changes)
        self._guest = await self._guest_manifest()
        self._host = host_now
        self._generation = self._vm.generation

    async def bring_out(self) -> BringOut:
        report = BringOut()
        if not self.is_in:
            return report
        guest_now = await self._guest_manifest()
        changes = diff(guest_now, self._guest)
        if changes:
            await self._vm.pull(self.key, self.root, changes, project=True)
            report.files = len(changes.changed) + len(changes.gone)
        self._guest = guest_now
        if self._git:
            await self._take_commits(report)
        self._host = await self._host_manifest()
        return report

    async def evict(self) -> BringOut:
        report = await self.bring_out() if self.is_in else BringOut()
        await self._vm.wipe(self.key)
        self.forget()
        return report

    def forget(self) -> None:
        self._generation = None
        self._host, self._guest = {}, {}
        self._branch = self._synced_sha = None

    # --- the files ------------------------------------------------------------------

    async def _host_manifest(self) -> Manifest:
        if not self._git:
            return await asyncio.to_thread(scan, self.root)
        names = await self._host_git_lines(
            "ls-files", "-z", "--cached", "--others", "--exclude-standard"
        )
        return await asyncio.to_thread(scan_names, self.root, _within(names))

    async def _guest_manifest(self) -> Manifest:
        if not self._git:
            return await self._vm.manifest(self.key)
        listed = await self._vm.call_checked(
            ["python3", self._gitsync, "files", self.guest_root], what="list the project"
        )
        names = _within({name for name in listed.split("\0") if name})
        return {k: v for k, v in (await self._vm.manifest(self.key)).items() if k in names}

    # --- the history ----------------------------------------------------------------

    async def _send_history(self, branch: str | None, sha: str, *, fresh: bool) -> None:
        source = f"refs/heads/{branch}" if branch else "HEAD"
        guest_bundle = "-"
        if sha:
            bundle = self._vm.scratch_path(".bundle")
            try:
                await self._host_git("bundle", "create", "-q", str(bundle), source)
                guest_bundle = await self._vm.put_path(bundle)
            finally:
                bundle.unlink(missing_ok=True)
        if sha or fresh:
            await self._vm.call_checked(
                [
                    "python3",
                    self._gitsync,
                    "init" if fresh else "update",
                    self.guest_root,
                    guest_bundle,
                    source,
                    branch or _DETACHED,
                    str(SANDBOX_UID),
                ],
                what="set up the project's history",
            )
        self._branch, self._synced_sha = branch, sha

    async def _take_commits(self, report: BringOut) -> None:
        raw = await self._vm.call_checked(
            ["python3", self._gitsync, "head", self.guest_root], what="read the project's HEAD"
        )
        try:
            guest = json.loads(raw)
        except ValueError as exc:
            raise VMError("the platform VM returned an unreadable HEAD") from exc
        branch = self._branch
        expected_ref = f"refs/heads/{branch or _DETACHED}"
        if guest.get("sha") == self._synced_sha:
            return
        if guest.get("ref") != expected_ref:
            report.notes.append(
                f"HEAD in the VM is on {guest.get('ref') or 'a detached commit'}, not "
                f"{expected_ref}; commits there were not brought back to the project."
            )
            return
        if branch is None or not self._synced_sha:
            report.notes.append(
                "The project has no branch checked out, so the commits made in the VM were "
                "not brought back."
            )
            return
        guest_out = f"{self._vm.layout.xfer}/{secrets.token_hex(8)}.bundle"
        result = await self._vm.call(
            ["python3", self._gitsync, "pack", self.guest_root, self._synced_sha, branch, guest_out]
        )
        if result.exit_code == 3:
            report.notes.append(
                f"{branch} was rewritten in the VM rather than added to, so its commits "
                "were not brought back. Only new commits on top of the branch return."
            )
            return
        if result.exit_code != 0:
            raise VMError(f"could not pack the new commits: {result.stderr.strip()[-300:]}")
        local = self._vm.scratch_path(".bundle")
        try:
            await self._vm.fetch_file(guest_out, local, max_bytes=_MAX_BUNDLE_BYTES)
            await self._land(local, branch, guest["sha"])
        finally:
            local.unlink(missing_ok=True)
        self._synced_sha = guest["sha"]
        report.commits_landed = True

    async def _land(self, bundle: Path, branch: str, sha: str) -> None:
        """Fetch the agent's commits, check they fast-forward the branch, and move it."""
        incoming = f"refs/odysseus/incoming-{secrets.token_hex(4)}"
        ref = f"refs/heads/{branch}"
        await self._host_git(
            "-c", "fetch.fsckObjects=true", "fetch", "-q", str(bundle), f"{ref}:{incoming}"
        )
        try:
            fetched = (await self._host_git("rev-parse", incoming)).strip()
            if fetched != sha:
                raise VMError("the commits the VM sent are not the ones it reported")
            current = (await self._host_git("rev-parse", ref)).strip()
            if current != self._synced_sha:
                raise VMError(f"{branch} moved on the host while the VM was working")
            await self._host_git("update-ref", ref, fetched, current)
            # The working tree already holds the committed files; bring the index along.
            await self._host_git("reset", "-q", "--mixed")
        finally:
            await self._host_git("update-ref", "-d", incoming, check=False)

    # --- host git ---------------------------------------------------------------------

    @property
    def _gitsync(self) -> str:
        return f"{self._vm.layout.opt}/gitsync.py"

    async def _is_repository_top(self) -> bool:
        top = await self._host_git("rev-parse", "--show-toplevel", check=False)
        return bool(top.strip()) and Path(top.strip()).resolve() == self.root.resolve()

    async def _host_head(self) -> tuple[str | None, str]:
        branch = await self._host_git("symbolic-ref", "-q", "--short", "HEAD", check=False)
        branch = branch.strip()
        sha = (await self._host_git("rev-parse", "-q", "--verify", "HEAD", check=False)).strip()
        return branch or None, sha

    async def _host_git_lines(self, *args: str) -> set[str]:
        out = await self._host_git(*args)
        return {name for name in out.split("\0") if name}

    async def _host_git(self, *args: str, check: bool = True) -> str:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(self.root),
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=git_config_pins(_git_env()),
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), _GIT_TIMEOUT_S)
        except TimeoutError as exc:
            proc.kill()
            raise VMError(f"`git {args[0]}` on the project timed out") from exc
        if check and proc.returncode != 0:
            raise VMError(f"`git {args[0]}` on the project failed: {err.decode().strip()[-300:]}")
        return out.decode("utf-8", "replace")


def _git_env() -> dict[str, str]:
    keep = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")
    return {name: os.environ[name] for name in keep if name in os.environ}


def _within(names: set[str]) -> set[str]:
    """``names`` and the directories above them, never anything under `.git`."""
    keep: set[str] = set()
    for name in names:
        parts = name.split("/")
        if parts[0] == ".git":
            continue
        keep.update("/".join(parts[: i + 1]) for i in range(len(parts)))
    return keep


@dataclass
class ProjectCopy:
    """One project folder's guest copy, and what is using it."""

    workspace: ProjectWorkspace
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    #: Background jobs started here and not yet stopped; a copy with any is never reaped.
    jobs: set[str] = field(default_factory=set)
    last_used: float = field(default_factory=time.monotonic)

    def touch(self) -> None:
        self.last_used = time.monotonic()


class ProjectCopies:
    """The project folders in the VM right now, by folder, each wiped once idle."""

    def __init__(self, vm: PlatformVM, *, idle_ttl_s: float, reap_interval_s: float) -> None:
        self._vm = vm
        self._idle_ttl_s = idle_ttl_s
        self._copies: dict[str, ProjectCopy] = {}
        self._reaper = PeriodicTask(
            "project-copy-reaper", interval_s=reap_interval_s, work=self._sweep, logger=logger
        )

    @property
    def vm(self) -> PlatformVM:
        return self._vm

    def acquire(self, root: Path) -> ProjectCopy:
        key = "p" + hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:20]
        copy = self._copies.get(key)
        if copy is None:
            copy = ProjectCopy(ProjectWorkspace(self._vm, key, root))
            self._copies[key] = copy
        copy.touch()
        return copy

    async def start(self) -> None:
        await self._reaper.start()

    async def stop(self) -> None:
        await self._reaper.stop()
        for key in list(self._copies):
            await self._evict(key)

    async def _sweep(self) -> None:
        now = time.monotonic()
        for key, copy in list(self._copies.items()):
            idle = now - copy.last_used >= self._idle_ttl_s
            if idle and not copy.jobs and not copy.lock.locked():
                await self._evict(key)

    async def _evict(self, key: str) -> None:
        copy = self._copies.pop(key, None)
        if copy is None:
            return
        async with copy.lock:
            try:
                await copy.workspace.evict()
            except VMError:
                logger.warning("project copy %s: could not bring the work out", key)
                copy.workspace.forget()
