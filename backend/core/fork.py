"""What forking a workspace needs, whichever kind of workspace it is.

A delegated agent does not share its parent's files: it gets a **fork** of them — its
own copy (sandbox) or its own branch in its own checkout (worktree) — works there, and
the parent decides afterwards what comes back. The two kinds differ in everything else,
and in exactly two things they must agree, which is why both live here:

**The copy has to be cheap, or nobody will fork.** A child that starts cold reinstalls
the ``.venv`` and ``node_modules`` its parent already built, and minutes of that in front
of every delegation is the difference between forking being the normal way to work and
being something the operator avoids. On darwin/APFS ``cp -c`` clones blocks
copy-on-write, so a gigabyte of dependencies costs metadata; elsewhere it degrades to a
real copy rather than to nothing.

**A merge back reports, it does not decide.** Both kinds answer the same three questions
— did it land, what came across, and what could not — because the caller that shows the
operator the answer must not have to ask a different question per workspace kind.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MergeReport:
    """What merging a forked workspace back into its parent did.

    ``merged`` is whether the child's work landed *in full*: a run with conflicts still
    carries whatever was applied in ``files``, so the report stays honest about a partial
    landing instead of collapsing it into a bare failure.

    ``deleted`` is separate from ``files`` because "your file changed" and "your file is
    gone" are not the same sentence to the operator reading the report, and a deletion is
    the one outcome a caller must never have to infer. The two kinds answer it
    differently, and neither can help it: a sandbox fork is a copy, so it reports its
    deletions and leaves the parent's files exactly as they are — the child dropping a
    file is not evidence the parent wanted it gone, and the parent may have been editing
    it the whole time. A worktree fork lands as a commit, which carries the deletion onto
    the parent's *branch*, where it stays reviewable and revertable like everything else
    in it — and still nowhere near the operator's own tree.
    """

    merged: bool
    files: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)


def clone_tree(source: Path, dest: Path) -> None:
    """Copy ``source`` to ``dest`` as cheaply as the filesystem allows.

    Blocking IO on a directory that may be gigabytes — call it off the event loop.

    ``dest`` must not exist: ``cp`` copies *into* an existing directory rather than
    becoming it, which would nest the tree one level deeper than every caller expects.
    A clone that fails halfway is thrown away and redone with a plain recursive copy —
    ``cp -c`` refuses outright on a volume with no clonefile support (a disk image, a
    non-APFS external drive), and a half-populated ``.venv`` is worse than a slow one.
    """
    if dest.exists():
        raise FileExistsError(f"{dest} already exists; a fork clone must create it")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        # Fixed argv, no shell: a workspace path is agent content and must never be
        # word-split or interpolated into a command line.
        done = subprocess.run(
            ["/bin/cp", "-c", "-R", str(source), str(dest)],
            capture_output=True,
            check=False,
        )
        if done.returncode == 0:
            return
        logger.info(
            "fork: copy-on-write clone of %s was refused (%s) — falling back to a copy",
            source,
            done.stderr.decode("utf-8", "replace").strip(),
        )
        shutil.rmtree(dest, ignore_errors=True)
    _ = shutil.copytree(source, dest, symlinks=True)
