"""The OS fence a worktree command runs inside, built per call from what it declared.

``host.py`` fences the *host* escape hatch — one profile, configured once, denying the
credential paths and the data directory. This builds a **narrower** profile for a command
the deterministic stage cleared (``services/permissions/judge.py``): writes confined to
the worktree, egress either off entirely or limited to the operator's allowed domains, and
the repository's own metadata protected by path rather than by a table of git subcommands.

**Why the fence is what made the allowlist unnecessary.** A read-only program table has to
answer "what will this binary do", and for the commands that matter it cannot: `git status`
runs whatever a repository's `.gitattributes` names as a clean filter, `git diff` runs
`diff.external`, and no flag and no environment pin states either rule (``gitenv.py`` says
why). Bounding what a *spawned program may do* answers all of them at once, and answers
them for `make`, `npm run` and a test suite too — none of which any table could have
enumerated. So the tier the judge assigns names a profile here, and the tool that executes
the call builds it: what runs can never exceed what was declared.

**It lives beside the host confinement rather than with the judge**, because it imports
``sandbox_runtime`` and reads the filesystem, and the judge is pure. The judge decides
*whether*; this decides *what the boundary is*.

**Deny beats allow in both runtimes**, and the profiles below are written for that. On
macOS the generated seatbelt profile emits every allow rule before every deny rule and the
last match wins (``sandbox_runtime/macos_sandbox.py``); on Linux a denied path is re-bound
read-only over the writable bind (``linux_sandbox.py``). So a narrow allow *inside* a broad
deny is not a thing that can be expressed — the sibling paths are listed explicitly
instead, and writes are deny-by-default, so what is not named is already denied.

**Linux binds paths that exist.** ``bwrap`` skips a bind whose source is missing, so a
lock file git has not created yet cannot be allowed by name there; the directory holding it
is allowed instead, which is wider (every branch ref rather than this thread's) and is the
platform's price for a `git commit` that works. macOS matches on the path string and needs
no such widening, so it does not get it.

**What this bounds, and what it does not.** Writes are deny-by-default with an allowlist,
and egress is allowlist-only — those two are the fence's, and they hold whatever a command
does once it is running. **Reads are not**: ``sandbox_runtime``'s filesystem config carries
a read *denylist* and no read allowlist at all, so a fenced command can still read anything
on the host outside the paths named in ``deny_read``. Reads are therefore bounded upstream
or not at all — the structural stage refuses a command that names a path outside the
worktree, and refuses a word it cannot place as a single path
(``services/permissions/shell_ast.py``). Nothing here is a second line under that one, and
a docstring implying otherwise would be the most expensive kind of wrong.

**Which is exactly why the tracked working directory is contained**, though not here.
The shell session remembers its directory between calls, and the structural stage measures
every relative path against the *worktree root* — so a command that moved the tracked
directory outside the worktree would leave every later containment claim measured against
the wrong place, with nothing downstream to catch the reads. The shell keeps that
containment in Python, where it is one `Path` comparison
(``shell_runner.FencedShell._apply_captured_cwd``); this file only wraps.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

# Re-exported rather than wrapped: the permission layer and the shell tool both ask this
# module whether the host can fence a process, and the answer is the same process-global
# singleton the escape hatch resolves (`host.resolve_confinement` — one sandbox per
# interpreter, configured once and cached). A second name in front of it said nothing the
# first did not. The `as` spelling is what marks them as this module's surface rather
# than as imports it forgot to use.
from .host import HostConfinement as HostConfinement
from .host import resolve_confinement as resolve_confinement
from .host import scratch_path

if TYPE_CHECKING:  # pragma: no cover - import-time cost, not behaviour
    from sandbox_runtime import SandboxRuntimeConfig

#: Files under the *common* git directory that a worktree command must never write, each
#: because writing it would let a command choose what a later command runs, or would edit
#: history the agent's own branch has no business touching. Named as paths rather than as
#: git subcommands: `git config`, a shell redirect and a Python script are the same write.
#:
#: ``refs/tags`` and ``refs/remotes`` are here and ``refs/heads`` is not, and the asymmetry
#: is forced: a commit has to write exactly one path under ``refs/heads``, deny beats allow
#: in both runtimes, and a deny on the parent would take the allow with it. See
#: :func:`workspace_profile` for what that leaves reachable in a plain checkout.
_PROTECTED_GIT = (
    "config",
    "hooks",
    "packed-refs",
    "info",
    "refs/tags",
    "refs/remotes",
    "logs/refs/remotes",
)


@dataclass(frozen=True)
class GitDirs:
    """Where a worktree's git metadata actually lives — its own, and the repository's.

    A linked worktree has two directories and they are usually not siblings:
    ``<root>/.git`` is a *file* pointing at a private directory under the main checkout
    (``…/.git/worktrees/<name>``), and that directory's ``commondir`` points at the shared
    one holding the objects, the refs and the config. A profile that fenced only the
    worktree would leave the operator's own repository wide open; one that denied the whole
    common directory would make `git commit` impossible. Both paths are needed, and reading
    them off the checkout is the only way to be right about a repository we did not create.
    """

    #: The worktree's own metadata: HEAD, the index, its reflogs. Writable — it is this
    #: thread's, and throwing the branch away throws it away too.
    private: Path
    #: The repository's shared metadata: objects, refs, config, hooks. Writable only in
    #: the few places a commit has to land.
    common: Path

    @classmethod
    def read(cls, root: Path) -> GitDirs | None:
        """The two directories for the checkout at ``root``, or None when it is not one.

        None is not an error: a workspace that is not a git checkout is fenced to its own
        directory and nothing else, which is the right answer and needs no git paths at all.

        **The pointer is checked against the repository it claims rather than followed on
        trust, and that check is a security boundary.** ``<root>/.git`` is a file *inside*
        the worktree, so a command this fence cleared can rewrite it — and what it names
        becomes the **next** command's write allowlist. Without a check, two commands that
        each stay inside the worktree (write a line, copy it over `.git`) hand a third an
        allow on any directory on the host, which is the one thing the fence exists to make
        impossible. The check is git's own layout: a linked worktree's private directory is
        always ``<common>/worktrees/<name>``, so both halves of the pointer have to agree
        about which repository this is, and making them agree about a directory we may not
        already write means first creating a directory *inside* it — the very write being
        attempted. A pointer that does not describe that shape contributes no git paths at
        all: a commit then fails inside the fence, visibly and redeclarably, which is the
        cheap half of the trade.

        **Every way of being wrong ends as ``None``, and the file's *bytes* are one of the
        ways.** This runs on every shell call of a thread and no caller above it catches
        anything, so a pointer that raises would stop the tool working at all rather than
        stop it believing one file — and a hostile pointer is exactly where that would be
        arranged. Two shapes raise rather than returning: bytes that are not UTF-8, and a
        path carrying a NUL, which the filesystem calls reject as a ``ValueError`` and not
        as an ``OSError``. So the reads are lenient about encoding and every error either
        kind is caught here, in one place, rather than at each call that might produce one.
        """
        try:
            return cls._read(root)
        except (OSError, ValueError):
            return None

    @classmethod
    def _read(cls, root: Path) -> GitDirs | None:
        """:meth:`read` without the degrade, so every failure has exactly one handler."""
        marker = root / ".git"
        if marker.is_dir():
            # A plain checkout: the metadata is inside the worktree, so it is exactly as
            # trustworthy as the worktree, and there is no pointer to be lied to by.
            return cls(private=marker, common=marker)
        pointer = marker.read_text(encoding="utf-8", errors="replace").strip()
        if not pointer.startswith("gitdir:"):
            return None
        private = Path(pointer.removeprefix("gitdir:").strip())
        if not private.is_absolute():
            private = (root / private).resolve()
        # A missing `commondir` raises past this method: it means this is not the
        # linked-worktree shape the check below can verify — a `--separate-git-dir`
        # checkout, a submodule, or a forgery. None of the three is what a worktree
        # creates, so none of them gets git paths.
        common_text = (private / "commondir").read_text(encoding="utf-8", errors="replace")
        common = Path(common_text.strip())
        if not common.is_absolute():
            common = (private / common).resolve()
        if not private.is_dir() or not common.is_dir():
            return None
        if private.parent.name != "worktrees":
            return None
        # Resolved on both sides because only one of them has been through `resolve()`
        # above, and on macOS `/tmp` and `/private/tmp` are the same directory under two
        # names — comparing the spellings would refuse every real worktree there.
        if private.parent.parent.resolve() != common.resolve():
            return None
        return cls(private=private, common=common)


def workspace_profile(
    root: Path,
    git: GitDirs | None,
    branch: str | None,
    *,
    allowed_domains: tuple[str, ...] | list[str] = (),
    deny_read: tuple[str, ...] | list[str] = (),
    allow_write: tuple[str, ...] | list[str] = (),
    linux: bool = False,
) -> SandboxRuntimeConfig:
    """The profile a command declared ``workspace`` or ``network`` runs under.

    Writable: the worktree itself, the OS temp root (a build, a test runner and the shell
    tool's own working-directory capture all need it), the four places a commit has to
    land — the object store, the worktree's private metadata, and this thread's branch ref
    and reflog — and ``allow_write``, the operator's own additions
    (``Settings.worktree_command_allow_write``).

    ``allow_write`` is a *separate* setting from the host hatch's, and the asymmetry is the
    point: a host command is one the operator read and approved, so its list may be as
    broad as `~`; a `workspace` command is one nobody was asked about, so the same breadth
    here would be the fence dissolved. What it is seeded with is the build caches, which
    are the difference between this tier clearing `uv run pytest` and `uv run pytest`
    failing to initialise its cache — see the setting for what is deliberately left out.

    Denied inside what *is* writable: the host escape hatch's scratch directory, the
    repository's config, hooks, packed refs, ``info``, tags and remotes, and the two
    pointer files this profile was itself derived from (:func:`_pointer_writes`). The
    scratch directory is the cwd every operator-approved host command starts in and
    resolves its relative paths against, so a command that needed no approval must not be
    able to leave something in it. The git paths are named as paths rather than as git
    subcommands: `git config`, a shell redirect and a Python script are the same write.

    **How far "the operator's other branches are out of reach" actually goes.** In a linked
    worktree — the shape a worktree thread always gets — the whole common directory is outside
    every allow, so another branch's ref is unreachable because nothing named it. In a plain
    checkout the common directory *is* ``<root>/.git``, inside the worktree allow, and the
    one ref a commit must write lives under ``refs/heads`` beside all the others: deny beats
    allow in both runtimes, so denying the parent would deny this thread's own branch too,
    and there is no third rule to write. So in that shape another branch stays writable, and
    saying so is better than a docstring that reads as a promise.

    ``allowed_domains`` is empty for ``workspace`` and the operator's list for ``network``.
    It can only *narrow*: the runtime filters proxied requests against the global config's
    own list, so a per-call list wider than that one buys nothing.

    ``deny_read`` is the host hatch's own list (``host.denied_read_paths``), restated
    because a per-call profile *replaces* the global one rather than narrowing it. Every
    entry is denied for writing too — read denial alone would still let a command clobber
    the vault or a key it could not read — mirroring what the host profile does.

    ``linux`` widens the branch-ref allows to their directories, because ``bwrap`` cannot
    bind a path that does not exist yet — see the module docstring.
    """
    from sandbox_runtime import FilesystemConfig, NetworkConfig, SandboxRuntimeConfig

    allowed = [str(root), tempfile.gettempdir(), *allow_write]
    deny_write = [str(scratch_path()), *deny_read]
    if git is not None:
        allowed += [str(git.common / "objects"), str(git.private)]
        allowed += _branch_writes(git.common, branch, linux=linux)
        # The lock, not the file. Every ref update takes `packed-refs.lock` to check
        # whether the ref it is moving has a packed copy to retire; with the lock denied
        # the commit still lands but git prints an error for it on every single commit,
        # which the model reads as a failed commit. Allowing the lock lets that check run;
        # rewriting `packed-refs` itself is still a rename onto a denied path, so
        # `git pack-refs` and a branch deletion fail exactly as before.
        allowed.append(str(git.common / "packed-refs.lock"))
        deny_write += [str(git.common / name) for name in _PROTECTED_GIT]
        deny_write += _pointer_writes(root, git)
    return SandboxRuntimeConfig(
        network=NetworkConfig(allowed_domains=list(allowed_domains)),
        filesystem=FilesystemConfig(
            deny_read=list(deny_read), allow_write=allowed, deny_write=deny_write
        ),
    )


def _pointer_writes(root: Path, git: GitDirs) -> list[str]:
    """The files this profile was *derived from*, denied so a command cannot rewrite them.

    :meth:`GitDirs.read` follows two pointers to find every git path above — the worktree's
    ``.git`` file, and the ``commondir`` inside the private directory it names — and both
    of those sit inside what this same profile makes writable (the worktree root; the
    worktree's own metadata). Rewriting one escapes nothing *now*; it changes what the
    **next** command's fence is built from, which is the same escape one call later. A
    fence whose own inputs are writable by what it fences is not a fence, so the inputs are
    denied. `GitDirs.read` refuses a pointer that does not describe this repository as
    well: this stops the rewrite, that stops a rewrite from being believed.

    A plain checkout has neither file — ``.git`` *is* the metadata directory there, written
    on every commit — and denying it would deny the commit with it. Nothing points anywhere
    in that shape, so there is nothing here to hold.
    """
    marker = root / ".git"
    if git.private == marker:
        return []
    return [str(marker), str(git.private / "commondir")]


def _branch_writes(common: Path, branch: str | None, *, linux: bool) -> list[str]:
    """The ref and reflog paths a commit on ``branch`` writes, and their lock files.

    A commit updates exactly one branch, so exactly one ref is opened for writing — and it
    is opened through a `.lock` beside it, which is why each path is named twice. With no
    branch known there is nothing to name: the command can still read and write the
    worktree, and a commit fails inside the fence with a note it can act on.
    """
    if not branch:
        return []
    refs = common / "refs" / "heads" / branch
    logs = common / "logs" / "refs" / "heads" / branch
    if linux:
        # The lock is created *in* the directory, and bwrap can only bind what exists.
        return [str(refs.parent), str(logs.parent)]
    return [str(refs), f"{refs}.lock", str(logs), f"{logs}.lock"]


#: The failures a denied write or a denied read surfaces as. Sniffed rather than read off
#: the runtime's own violation record, because that record is filled by a macOS-only kernel
#: log monitor which has to be started at global initialisation and does not observe every
#: denial even then — so a note that depended on it would be a note that never appeared.
#: A false positive costs one advisory line under an unrelated permission error.
_DENIALS = ("Operation not permitted", "Permission denied", "Read-only file system")

_FENCE_NOTE = (
    "\n\n[fence] This command ran inside an OS fence built from the reach it declared: it "
    "may write the worktree and reach only the domains that reach allows. A permission "
    "failure above is that boundary, not a broken command — work inside the worktree, or "
    "declare the reach this command actually needs and run it again."
)


async def wrap(command: str, profile: SandboxRuntimeConfig) -> str:
    """``command``, rewritten to run under ``profile``.

    Nothing more than that. The command arrives already carrying whatever the caller needs
    to happen *inside* the fence — :class:`services.sandbox.shell_runner.FencedShell`
    appends its own working-directory capture before calling here, so the capture runs
    within the boundary and the recorded path is checked in Python afterwards rather than
    by a shell construct out here.

    That is worth stating because the alternative was tried: while the shell tool was built
    on a harness that spawned processes for itself, the working-directory suffix landed
    *outside* the fence and this function grew a `case`-guarded `cd` to carry it back in.
    Owning the spawn removed the need for it. A shim in a generated shell string is the
    kind of thing that is easy to add here and hard to reason about later, so the rule is
    that this function wraps and does nothing else.
    """
    from sandbox_runtime import SandboxManager

    return await SandboxManager.wrap_with_sandbox(command, custom_config=profile)


def annotate(output: str) -> str:
    """``output`` with a line under it saying that a permission failure was the fence.

    A denied write surfaces to the command as a bare `Operation not permitted`, which reads
    as a broken tool rather than as a boundary — and the model's next move on a broken tool
    is to try it again, or to work around it. The note is what lets it see the fence and
    redeclare its reach instead. Appended to the whole tool result rather than to a stderr
    stream of its own, because the shell tool hands back one labelled string and splitting
    it apart to re-join it would invent a seam.

    **Written here rather than read off the runtime**, which is the part worth stating.
    ``sandbox_runtime`` does keep a violation store, and it is filled by a macOS-only
    monitor over the kernel log that must be enabled when the process-wide sandbox is
    initialised — it records nothing on Linux, and on macOS it did not record an ordinary
    denied write when it was tried. A note nobody ever sees is worse than no note, so this
    one is deterministic: it says what the boundary *is* rather than which rule fired, and
    it is right on both platforms.
    """
    return output + _FENCE_NOTE if any(denial in output for denial in _DENIALS) else output
