"""The files a worktree thread can name — what the composer's `@` picker browses.

**Operator-facing only, and agent-unreachable by construction.** The agent already reaches
this filesystem through ``files_list_directory`` and ``files_search_files``, rooted at the
workspace its run resolved; nothing under ``tools/`` or ``agent/`` may import this module,
and ``tests/test_host_surface_guard.py`` fails if anything does. What lives here is the
*other* direction — the operator picking a path to hand to the agent — which is a listing
of their own checkout and has no business being reachable from inside a turn.

**Git first, a bounded walk second.** ``git ls-files --cached --others
--exclude-standard`` answers in one call with tracked plus untracked-not-ignored files,
which means ``.gitignore`` is honoured for free and ``node_modules`` is never walked at
all. That last part is not an optimisation: a naive walk of a working repository spends
most of its time inside directories the operator would never reference. The fallback
exists for a directory that is not a repository yet — ``Project.git_initialized`` records
which — and is bounded in both depth and entries, because a picker that hangs is worse
than one that says it truncated.

**Ranking is here, not in the client.** Basename-prefix, then basename-substring, then
path-substring, in exclusive buckets — the same shape ``rankCommands`` and
``searchSettings`` keep. A path is mostly directory names, so a query matched anywhere in
one would bury the file actually called that under every file beneath a directory that
merely mentions it.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path

from services.sandbox.base import contained_file

from .repo import branch_for, run_git

#: How deep the non-git walk goes. Deep enough for an ordinary source tree, shallow
#: enough that a stray symlink into a large filesystem costs a bounded amount.
_MAX_DEPTH = 8

#: How many entries the non-git walk will visit before giving up and reporting truncation.
#: Git needs no equivalent — it returns the repository's own index, which is already the
#: bounded thing.
_MAX_SCANNED = 20_000

#: Directories the fallback never descends into. Short and deliberately not a policy: with
#: no `.gitignore` to read, these are the ones whose absence from a file picker nobody has
#: ever regretted.
_SKIP = frozenset(
    {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".next"}
)


@dataclass(frozen=True)
class FileEntry:
    """One file, named relative to the root it was listed from."""

    path: str
    name: str


@dataclass(frozen=True)
class FileListing:
    entries: tuple[FileEntry, ...]
    #: True when the scan hit its bound, so the picker can say the list is partial rather
    #: than implying the repository simply has no more files.
    truncated: bool


async def worktree_root(
    worktree_path: Path, conversation_id: str, project_root: Path
) -> tuple[Path, bool]:
    """Which filesystem the picker should list, and whether it is the thread's worktree.

    **Not the holder map.** ``WorktreeManager._holders`` is an in-memory dict written only
    by ``acquire``, so after a backend restart a worktree that is still very much on disk
    has no holder — and a picker keyed on one would quietly fall back to the operator's own
    checkout while the agent's first file call re-acquires the worktree. The operator would
    pick ``src/foo.ts`` from a tree the agent cannot see, and nothing would report it.

    So the question is asked of the filesystem, which is where the answer actually lives:
    the worktree directory exists, and the branch checked out in it is this conversation's.
    Both halves matter — one worktree serves a project, so an existing directory on
    *another* thread's branch is not this thread's files.

    **It never creates one.** A picker that acquired a checkout as a side effect of typing
    ``@`` would take the project's single worktree away from whatever else holds it. Before
    a thread's first turn there is nothing to list but the project root, which is the
    honest answer and also the useful one.
    """
    if not worktree_path.is_dir():
        return project_root, False
    code, out, _ = await run_git(worktree_path, "rev-parse", "--abbrev-ref", "HEAD")
    if code != 0 or out.strip() != branch_for(conversation_id):
        return project_root, False
    return worktree_path, True


async def list_files(root: Path, *, query: str = "", limit: int = 200) -> FileListing:
    """The files under ``root`` matching ``query``, best first."""
    paths, truncated = await _candidates(root)
    ranked = _rank(query, paths)
    entries = tuple(FileEntry(path=p, name=p.rsplit("/", 1)[-1]) for p in ranked[:limit])
    return FileListing(entries=entries, truncated=truncated or len(ranked) > limit)


@dataclass(frozen=True)
class FileContent:
    """One file's bytes, and whether that is all of them."""

    data: bytes
    #: True when the read stopped at its ceiling. Said out loud for the same reason
    #: :attr:`FileListing.truncated` is — a viewer showing the first megabyte of a file
    #: must not look like a viewer showing the file.
    truncated: bool


async def read_file(root: Path, relpath: str, *, max_bytes: int) -> FileContent | None:
    """The bytes of ``relpath`` under ``root``, or ``None`` when there is no file to read.

    The operator-facing counterpart to :func:`list_files`: they picked a path out of that
    listing and want to look at it. Reads through :func:`~services.sandbox.base.contained_file`
    rather than joining the path itself, which is what refuses a traversal, a symlink out of
    the tree, and a directory — and refuses them as an *answer* rather than an exception,
    because the operator is browsing and a path that no longer resolves is an ordinary
    outcome rather than a fault.

    ``None`` covers every one of those cases on purpose. Telling the operator which of
    "outside the tree", "a symlink", "a directory" or "not there" their path was would be
    describing the filesystem around a path they are not allowed to read.

    Bounded, because nothing upstream is: ``list_files`` honours ``.gitignore`` and so
    never *offers* a build artifact, but a path can be asked for directly and a worktree
    holds whatever the agent put in it.
    """
    target = contained_file(root, relpath)
    if target is None:
        return None
    # +1 so the ceiling is detected rather than inferred: a file exactly `max_bytes` long
    # would otherwise be indistinguishable from one that was cut at it.
    data = await asyncio.to_thread(_read_head, target, max_bytes + 1)
    if data is None:
        return None
    if len(data) > max_bytes:
        return FileContent(data=data[:max_bytes], truncated=True)
    return FileContent(data=data, truncated=False)


def _read_head(target: Path, limit: int) -> bytes | None:
    """``limit`` bytes off the front of a file, or ``None`` if it cannot be read.

    An unreadable file is the same ordinary outcome a missing one is — a permission bit
    the operator has not noticed is not worth a 500.
    """
    try:
        with target.open("rb") as handle:
            return handle.read(limit)
    except OSError:
        return None


async def _candidates(root: Path) -> tuple[list[str], bool]:
    code, out, _ = await run_git(
        root, "ls-files", "--cached", "--others", "--exclude-standard", "-z"
    )
    if code == 0:
        # NUL-separated, so a filename containing a newline survives the round trip.
        # git reports paths relative to the repository root and always with forward
        # slashes, which is the shape the rest of this module assumes.
        return [name for name in out.split("\0") if name], False
    return _walk(root)


def _walk(root: Path) -> tuple[list[str], bool]:
    """A bounded, ignore-free walk — for a directory git will not answer for.

    **Both bounds report.** The entry cap is the obvious one, but a subtree left unread
    because it sits past ``_MAX_DEPTH`` is exactly as invisible, and a listing that says it
    is complete when it is not sends the operator looking for a file the picker decided not
    to mention. Saying "truncated" is what lets them reach for a full path instead.
    """
    found: list[str] = []
    scanned = 0
    truncated = False
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        directory, depth = stack.pop()
        try:
            with os.scandir(directory) as it:
                for entry in it:
                    scanned += 1
                    if scanned > _MAX_SCANNED:
                        return found, True
                    if entry.name.startswith(".") or entry.name in _SKIP:
                        continue
                    # `follow_symlinks=False`: a link out of the tree would otherwise be
                    # descended into, and containment is checked against `root`.
                    if entry.is_dir(follow_symlinks=False):
                        if depth < _MAX_DEPTH:
                            stack.append((Path(entry.path), depth + 1))
                        else:
                            truncated = True
                        continue
                    if entry.is_file(follow_symlinks=False):
                        found.append(Path(entry.path).relative_to(root).as_posix())
        except OSError:
            # A directory that vanished or cannot be read costs its own subtree and
            # nothing else — the operator is typing, and half a list beats an error. It is
            # still a subtree the listing does not contain, so it counts as truncation.
            truncated = True
            continue
    return found, truncated


def _rank(query: str, paths: list[str]) -> list[str]:
    """Exclusive buckets: the file *called* that, then one whose name contains it, then
    one merely living under a directory that does."""
    q = query.strip().lower()
    if not q:
        return sorted(paths)
    by_name_prefix: list[str] = []
    by_name: list[str] = []
    by_path: list[str] = []
    for path in paths:
        name = path.rsplit("/", 1)[-1].lower()
        if name.startswith(q):
            by_name_prefix.append(path)
        elif q in name:
            by_name.append(path)
        elif q in path.lower():
            by_path.append(path)
    return [*sorted(by_name_prefix), *sorted(by_name), *sorted(by_path)]


