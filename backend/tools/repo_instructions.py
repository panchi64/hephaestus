"""How much of a project's own `CLAUDE.md`/`AGENTS.md` reaches the prompt head.

These files land in the standing brief — cache-stable, re-sent every turn, ahead of the
conversation — which is exactly what makes them worth loading and exactly what makes an
unbudgeted load dangerous. A repository whose instruction file has grown to a couple of
hundred kilobytes (they do; they accrete) silently spends the whole window before the
model has read a single message, and the only visible symptom is that long threads start
compacting early. The capability that loads them has no cap of its own, so the cap lives
here, on our side of the seam.

**Whole files are dropped before any file is cut.** Discovery hands them back
broadest-first and most-specific-last, which is also least-to-most relevant: the file
sitting in the directory the agent is actually working in is the one whose sentences it
needs, and half a broad file is worth less than none of it plus all of a narrow one. Only
when the most specific file *alone* overruns the budget is anything truncated — and then
it is truncated with a visible marker at a line boundary, because a brief that stops
mid-sentence reads as a corrupted instruction rather than a shortened one.

Bytes rather than tokens because that is what is actually being defended (a UTF-8 file on
disk) and because a byte cap needs no tokenizer to be exact. The number is a budget, not
an estimate, so nothing here is scaled or approximated.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from pydantic_ai.workspaces import LocalWorkspaceBackend, Workspace

# Discovery, dedup and rendering are the capability's own, reached through its loader
# module: re-deriving them here would mean a second implementation of symlink dedup and
# of the `<context-file>` framing, and the two would disagree the first time either moved.
from pydantic_ai_harness.repo_context._loader import (
    ContextFile,
    discover_instruction_files,
    render_context_files,
)

#: Instruction filenames, in within-directory precedence order — the capability's own
#: default, restated because the budget is applied over the same discovery.
INSTRUCTION_FILENAMES = ("CLAUDE.md", "AGENTS.md")

#: How many bytes of instruction files may reach the brief. 16KB is roughly 4k tokens —
#: an eighth of a 32k window spent before the first message, and generous for the thing it
#: describes (how to work in this repository), which is the shape a budget should have:
#: invisible to every reasonable file, decisive against the one that has stopped being
#: reasonable. It is charged on *every* request of every turn, so a budget that merely
#: looks small against a large window is still the wrong size.
INSTRUCTIONS_BYTE_BUDGET = 16 * 1024

#: What replaces the cut tail. Addressed to the model, because the model is who has to
#: understand that the file continues and it has not been told the rest.
_TRUNCATION_NOTE = (
    "\n\n[This instruction file was cut here: it is larger on its own than the "
    "instruction budget for a request. Read the rest of it directly if you need it.]"
)


def _size(file: ContextFile) -> int:
    return len(file.content.encode("utf-8"))


def _truncate(file: ContextFile, budget: int) -> ContextFile:
    """The file cut to fit, at the last line boundary inside the budget, with the note
    appended. A cut that lands mid-line is worse than useless in an instruction file —
    half a rule reads as a whole one."""
    room = max(0, budget - len(_TRUNCATION_NOTE.encode("utf-8")))
    head = file.content.encode("utf-8")[:room].decode("utf-8", errors="ignore")
    newline = head.rfind("\n")
    if newline > 0:
        head = head[:newline]
    return replace(file, content=head + _TRUNCATION_NOTE)


def within_budget(files: list[ContextFile], budget: int) -> list[ContextFile]:
    """The files that fit, most-specific-last order preserved.

    Drops whole files from the *broad* end while the set overruns, which is the half of
    the rule that matters: dropping is lossless for what remains, truncating is not. The
    survivor is cut only if it is the last one standing and still too big.

    Each file is measured once and the running total is adjusted as files leave, rather
    than re-summed per iteration: the measurement is a full UTF-8 encode, and re-encoding
    a monorepo's whole instruction set once per dropped file is quadratic work over
    exactly the oversized input this function exists to handle.
    """
    kept = list(files)
    sizes = [_size(f) for f in kept]
    total = sum(sizes)
    while len(kept) > 1 and total > budget:
        total -= sizes.pop(0)
        kept.pop(0)
    if kept and sizes[0] > budget:
        kept[0] = _truncate(kept[0], budget)
    return kept


async def repo_instruction_text(root: Path, *, budget: int = INSTRUCTIONS_BYTE_BUDGET) -> str:
    """The project's instruction files, budgeted and rendered, or ``""`` when it has none.

    ``home_dir=None``: the walk-up is deliberately not taken (see ``tools/repo.py``), so
    what is discovered here is what the worktree root itself carries.
    """
    workspace = Workspace(LocalWorkspaceBackend(root))
    files = await discover_instruction_files(workspace, root, None, INSTRUCTION_FILENAMES)
    if not files:
        return ""
    return render_context_files(within_budget(files, budget), relative_to=root)
