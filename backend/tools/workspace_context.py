"""What the agent already has on disk, told to it once per turn.

The model's own file tools report only the call they just made — `code_execute`
returns two streams, `files_write_file` returns a byte count — and nothing has ever
put the *state* of the workspace in front of it. So knowing what it had already built
depended entirely on the tool calls still being in the replayed history, and a
compaction (`agent/summarize.py`) replaces that stretch with a summary. Past the fold,
a conversation's own files became invisible to it, and it rebuilt them.

This block is the fix, and it is deliberately the dullest possible one: read the
directory, list what is in it, every turn. It is derived from disk rather than
remembered, so it is correct after a fold, after a reap, after a restart, and after a
turn that the model itself has forgotten.

**It goes at the tail, and that placement is the whole cost argument.** A file listing
changes on nearly every step, and a changing block at the *head* of the request
invalidates the inference engine's prompt-prefix cache from byte 0 for the entire
conversation behind it (`tools/deps.py`, `agent/factory.py`). At the tail it is a few
hundred tokens on the turn it rides, and the history in front of it stays byte-stable.

**It creates nothing.** Not a session, not a directory — see `settled_workspace`. A
turn that never touches a file must not pay for a session's worth of bookkeeping to
be told there is nothing to say. And it runs **off the event loop**: the walk is
ordinary blocking IO over a directory the agent controls the size of, so it goes
through a thread like every other workspace walk in this codebase.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from services.sandbox import SandboxSessionManager
from services.sandbox.walk import excluded, walk_files
from services.workspace import SANDBOX_MOUNT, scratch_key

from .deps import PromptContextRequest

# Rendered above the listing, in two forms. The claim of completeness is the whole
# value of the block — a model that believes the list is partial gains nothing from it
# — but it is also the one sentence that must never be made when it is false: a model
# told "this is everything", finding no `report.md` in it, concludes the file was never
# written and writes it again, which is the exact failure this block exists to prevent.
# So the second wording is used whenever anything was capped or pruned.
_PREAMBLE_WHOLE = (
    "Files already in your scratch, read from disk just now. This is the whole of it "
    "— work you did earlier in this conversation is here, including work from before "
    "anything you can still see in the history above. Read a file before assuming what "
    "is in it, and do not rebuild something that is already listed."
)
_PREAMBLE_PARTIAL = (
    "Files already in your scratch, read from disk just now. This is a sample, not "
    "the whole of it — work you did earlier in this conversation is here, including "
    "work from before anything you can still see in the history above. List a "
    "directory before concluding a file is missing, and do not rebuild something that "
    "is already listed."
)

# How many entries the listing names before it starts counting instead. A workspace with
# a few hundred files is ordinary once a build has run; one with ten thousand is a
# `node_modules` the walk already prunes, or a checkout, and neither is worth a token.
# Chosen to stay a few hundred tokens at the tail rather than to be a real ceiling on
# what the agent may keep.
MAX_ENTRIES = 200

# How many entries the walk *visits* before it gives up counting. Separate from the cap
# above and much larger, because the two bound different costs: `MAX_ENTRIES` bounds
# the tokens, this bounds the syscalls. Without it a workspace holding a downloaded
# dataset would be stat'ed in full, every turn, to render two hundred lines —
# `collect_text_files` bounds the same walk at 2,000 for the same reason.
_MAX_SCAN = 5_000

# Directories the walk prunes but the model still needs to know it *has*, named without
# being listed. They are pruned because listing them is thousands of lines of files the
# agent did not write — and they are named because each one is minutes of work it would
# otherwise do twice: reinstalling a virtualenv it already has, rebuilding a `dist/` it
# already built, re-cloning a repository whose history is right there. This is the same
# mistake the seal used to make by dropping them silently, and naming them is the cheap
# half of not making it. Caches are deliberately absent: nothing the agent decides turns
# on whether `__pycache__` exists.
_WORTH_NAMING = (".venv", "venv", "node_modules", ".git", "dist", "build")


def _human(size: int) -> str:
    """A file's size in the shortest honest form. Bytes up to a kilobyte, then one
    decimal — precision below that is noise in a block the model skims."""
    if size < 1024:
        return f"{size}B"
    for unit, scale in (("K", 1024), ("M", 1024**2), ("G", 1024**3)):
        if size < scale * 1024 or unit == "G":
            return f"{size / scale:.1f}{unit}"
    return f"{size}B"  # unreachable; the loop returns at G


def _listing(root: Path, excludes: tuple[str, ...]) -> tuple[str, bool]:
    """The workspace as ``path  size`` lines, and whether anything was left out.

    Lines are sorted by **path**, so the common case reads like a directory and two
    consecutive turns over the same files produce the same block. The *cap* is applied
    by modification time instead — if something has to be dropped, what the agent
    touched most recently is what it is most likely to be working on.

    The flag is what decides which preamble the block carries, so it has to account for
    both ways this falls short of the whole truth: the display cap, and the scan cap
    that stops the walk before it has seen everything.
    """
    entries: list[tuple[float, str, int]] = []
    scan_capped = False
    for rel, full in walk_files(root, excludes):
        if len(entries) >= _MAX_SCAN:
            scan_capped = True
            break
        try:
            stat = full.stat()
        except OSError:  # vanished mid-walk — the agent's own process may be writing
            continue
        entries.append((stat.st_mtime, rel, stat.st_size))
    if not entries:
        return "", scan_capped

    total = len(entries)
    entries.sort(key=lambda e: e[0], reverse=True)
    kept = entries[:MAX_ENTRIES]
    lines = [
        f"{SANDBOX_MOUNT}/{rel}  {_human(size)}"
        for _mtime, rel, size in sorted(kept, key=lambda e: e[1])
    ]
    if total > len(kept) or scan_capped:
        # Naming the directories rather than only the count: "and 4,812 more" tells the
        # model nothing it can act on, while the directories tell it where to look.
        elided = sorted(
            {rel.split("/", 1)[0] for _m, rel, _s in entries[MAX_ENTRIES:] if "/" in rel}
        )
        where = f" (mostly under {', '.join(elided[:6])})" if elided else ""
        # "at least", because past the scan cap the count is a floor rather than a total.
        count = f"at least {total - len(kept)}" if scan_capped else str(total - len(kept))
        lines.append(f"… and {count} more{where} — list a directory to see them.")
    return "\n".join(lines), total > len(kept) or scan_capped


def _also_present(root: Path, excludes: tuple[str, ...]) -> str:
    """The pruned directories worth naming, if any are there. See `_WORTH_NAMING`.

    Filtered through the same :func:`~services.sandbox.walk.excluded` the listing walks
    by, rather than trusting `_WORTH_NAMING` alone: the exclusions are an operator
    setting, and a `dist/` they chose to make visible would otherwise be listed *and*
    announced as "not listed" in the same block.
    """
    found = [name for name in _WORTH_NAMING if excluded(name, excludes) and (root / name).is_dir()]
    if not found:
        return ""
    return (
        f"Also present, not listed: {', '.join(f'{n}/' for n in found)}. "
        "You already have these — don't reinstall, rebuild or re-clone them."
    )


async def workspace_context(req: PromptContextRequest) -> str:
    """This conversation's scratch files, for the tail of this turn's prompt.

    Empty — no block at all — in the two cases where it would be noise: a thread that has
    never put a file in its scratch, and one whose sandbox is unavailable. That is the
    same discipline `tasks_context` follows for an empty task list: a heading over nothing
    is a heading the model has to read. A project is never listed: it is the operator's
    repository, with git to say what is in it.
    """
    sessions = req.caps.get_optional(SandboxSessionManager)
    if sessions is None:
        return ""
    root = sessions.settled_workspace(scratch_key(req.workspace_key))
    if root is None:
        return ""
    # The manager's own tuple, not a fresh read of the setting: `walk.py` is one answer
    # four readers must agree on, and a second source that merely happens to match is
    # how they drift.
    #
    # Off the loop: a walk over a directory whose size the *agent* decides is not
    # something to run inline in the turn prelude, where it would stall every other
    # conversation's stream for as long as it takes. Same seam `tools/view.py` puts
    # `collect_text_files` behind, for the same reason.
    return await asyncio.to_thread(_render, root, sessions.walk_excludes)


def _render(root: Path, excludes: tuple[str, ...]) -> str:
    """The block, or ``""``. Blocking IO — call it off the event loop."""
    listing, partial = _listing(root, excludes)
    also = _also_present(root, excludes)
    if not listing and not also:
        return ""
    # Anything pruned makes the listing a sample, whatever the caps did: the directories
    # named below are real files the block does not show.
    preamble = _PREAMBLE_WHOLE if not (partial or also) else _PREAMBLE_PARTIAL
    body = "\n\n".join(part for part in (listing, also) if part)
    return f"{preamble}\n\n{body}"
