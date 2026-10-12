"""Filesystem tools over the agent's own sandbox workspace (`AE-2` Filesystem).

The agent's other way to touch files is ``code_execute``, which means editing by heredoc
and reading by ``cat``. That works and is miserable: every edit rewrites a whole file
through a shell, and every read costs a round-trip into the VM. These tools are the direct
route — read a slice, replace an exact span, grep, glob — so the model spends its turn on
the problem rather than on shell quoting.

**We do not hand-roll them.** ``pydantic_ai_harness.FileSystem`` is the Pydantic team's
own capability: eight tools whose containment (``..``, absolute paths, and symlinks that
``realpath`` outside the root are all rejected), binary detection, hash-checked edits and
``ModelRetry`` errors are theirs to maintain. We supply one thing they cannot know — which
directory this run may touch.

**Which directory is per-run, and the category object is not.** Categories are assembled
once at app startup (see ``app.py``) and shared by every conversation, while the workspace
belongs to the run. So the root cannot be baked in at construction:
this category is a :class:`~tools.rebound.WorkspaceToolset`, which asks
``tools/workspace.py``'s resolver per call and hands that directory to the harness
toolset as ``ctx.workspace``.

**The invariant is that these tools reach exactly what this run can execute in, and
nothing else.** In a sandbox mode that is the conversation's sandbox workspace at
``<data_dir>/sandbox/work/<key>/`` — the host copy of what appears as ``/work`` in the VM,
the box's own scratch space, never the operator's files — so they reach exactly
what ``code_execute`` reaches. In a worktree it is the project's git worktree, which is
exactly what the shell tools run in. Either way editing and running agree, which is the
whole point of resolving the workspace once (``services/workspace.py``).

They are still not host filesystem tools: a worktree is a throwaway branch checkout, and
the operator's own tree is only ever written by an explicit, approval-gated merge. Being
confined either way, they need no approval, exactly as sandboxed execution doesn't.
"""

from __future__ import annotations

from pydantic_ai import AbstractToolset
from pydantic_ai_harness import FileSystem

from core.config import get_settings

from .deps import RunDeps
from .rebound import WorkspaceToolset

# Deliberately *not* derived from the seal's exclusion list. Marking those paths read-only
# looked like a kindness — the model can't invest edits in files a reap will drop — but the
# list includes `dist` and `build`, which is where a build the agent just ran puts its
# output. It would have been refused `files_edit_file` on its own artifact while the same
# edit through `code_execute`'s shell succeeded: an asymmetry with no rule behind it, which
# reads to the model as a random failure. The harness's own defaults (`.git/*`, `.env`,
# key files, `**/secrets*`) stay in force; they protect things worth protecting.


def files_toolset() -> AbstractToolset[RunDeps]:
    """The ``files`` category, built once at app assembly and shared by every run."""
    toolset = FileSystem[RunDeps](
        max_read_lines=get_settings().sandbox_files_max_read_lines,
    ).get_toolset()
    assert toolset is not None
    return WorkspaceToolset("files", toolset)
