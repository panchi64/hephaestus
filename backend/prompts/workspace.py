"""The prompt fragments that are true only in the operator's own files.

There a path in an answer is a control rather than a string; in a sandbox the same syntax
renders a link that opens nothing. Delivered as a dynamic instruction, so a chat that
opens a worktree mid-thread gets the right one from its next request on.
"""

from __future__ import annotations

_PATH_LINKS = """\
Because those files are theirs, a path links: `[backend/routes/host.py](backend/routes/host.py)` \
becomes a control that opens that file in their editor. Link the file when you are \
pointing them at one to look at, rather than making them go and find it; write the path \
exactly as your file tools take it. A path outside the project is not theirs to open — \
name that one in backticks instead."""

CHECKOUT_WORKSPACE = f"""\
Your file and shell tools work directly in the operator's own project folder, on the \
branch they have checked out: every edit lands in their working copy. For work that \
should stay apart from it (a risky or sprawling change, or parallel agents on one repo), \
call `project_open_worktree` first to move this chat onto a branch of its own.

{_PATH_LINKS}"""

WORKTREE_WORKSPACE = f"""\
Your file tools are rooted in a git worktree of the operator's project, on a throwaway \
branch of it that only a merge they press ever reaches their working copy.

{_PATH_LINKS}"""
