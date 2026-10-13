"""Files the operator named with `@` — a reference, never an injection.

The turn carries the **paths** and nothing else. The model reads what it needs with
``files_read_file``, which is classified a read (``services/tool_sensitivity``) and so
costs no approval at any level, and pages through a large file itself rather than having
its text poured into the window.

That is the same call the attachment path already makes, and for the stated reason: the
inline-text cap was removed from this codebase in favour of handing the model a file with
a path. A reference also cannot go stale in history — an `@` from twenty turns ago names
a file that has since changed, and a copy of its old contents replaying forever would be
worse than useless.

**Resolved against the run's own workspace**, not against whatever the picker listed. The
two are normally the same tree, but a thread whose worktree was created between the pick
and the send would otherwise carry a path from the operator's checkout into a turn that
cannot see it. A path that does not resolve is dropped rather than reported: the operator
still has their typed message, and a turn refused over a file they can simply mention
again is a worse trade than a turn that runs with one fewer reference.
"""

from __future__ import annotations

from services.conversation_view import FILE_REFS_MARKER_OPEN
from services.sandbox.base import contained_file
from services.workspace import RunWorkspace

#: How many references one turn may carry. Not a context bound — each is a path, not a
#: file — but a message naming two hundred files is asking the model to do something no
#: single turn can do, and the cap makes that visible instead of quietly expensive.
MAX_FILE_REFS = 40


def resolve_file_refs(paths: list[str], workspace: RunWorkspace | None) -> tuple[list[str], str]:
    """The references that resolve, and the block naming them for the model."""
    if not paths or workspace is None:
        return [], ""
    root = workspace.root
    kept: list[str] = []
    # The cap counts what is *kept*, not what was offered: a list carrying the same path
    # twice would otherwise spend the budget on it and push a distinct reference out, which
    # is the operator losing a file they named to a duplicate they did not notice.
    for relative in paths:
        if len(kept) >= MAX_FILE_REFS:
            break
        if relative not in kept and contained_file(root, relative) is not None:
            kept.append(relative)
    return kept, _marker(kept, workspace)


def _marker(paths: list[str], workspace: RunWorkspace) -> str:
    """The trusted block naming the referenced files.

    Carries no untrusted fence, because it contains no file *content* — it is the chassis
    telling the model which paths the operator pointed at. The paths are given as the
    workspace displays them, so they are the strings the file tools take.
    """
    if not paths:
        return ""
    lines = "\n".join(f"- {workspace.display(path)}" for path in paths)
    return (
        f"{FILE_REFS_MARKER_OPEN}\n"
        f"{lines}\n\n"
        "Read one with files_read_file when you need it — they were named to point you "
        "at the work, not to be summarised back. Nothing here has been read for you.]"
    )
