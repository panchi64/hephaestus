"""A project's own instruction files, handed over the first time a thread touches it.

A project's `CLAUDE.md` and `AGENTS.md` say how to work in it, so they reach the agent with
the first file or shell result that lands in the project, rather than in the standing
brief, which stays the same in every thread and keeps the inference engine's cached
prefix. A project counts as briefed while its brief is still in the history the run
replays: once a compaction folds it away, the next touch hands it over again.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from pydantic_ai import RunContext
from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart

from services.home.paths import HOME, Place
from services.home.table import RunHome

from .deps import RunDeps
from .repo_instructions import repo_instruction_text

#: The key a dict result carries the brief under.
BRIEF_KEY = "project_instructions"


def _header(place: Place) -> str:
    return f"How to work in ~{place.path[len(HOME) :]}, from its own instruction files:"


async def brief_for(ctx: RunContext[RunDeps], home: RunHome, places: Iterable[Place]) -> str:
    """The instruction files of each project or worktree in ``places`` this thread has not
    been shown yet, or ``""``."""
    briefs: list[str] = []
    for place in places:
        if place.kind not in ("projects", "worktrees") or place in ctx.deps.briefed:
            continue
        ctx.deps.briefed.add(place)
        header = _header(place)
        mount = home.mount(place)
        if mount is None or _shown(ctx.messages, header):
            continue
        text = await repo_instruction_text(mount.root)
        if text:
            briefs.append(f"{header}\n\n{text}")
    return "\n\n".join(briefs)


async def with_brief(
    ctx: RunContext[RunDeps], home: RunHome, places: Iterable[Place], result: object
) -> object:
    """``result`` carrying the brief for ``places`` appended, when it is text. A result of
    any other shape is handed back as it is, and its places stay unbriefed."""
    if not isinstance(result, str):
        return result
    brief = await brief_for(ctx, home, places)
    return f"{result}\n\n{brief}" if brief else result


async def with_brief_payload(
    ctx: RunContext[RunDeps], home: RunHome, places: Iterable[Place], payload: dict[str, object]
) -> dict[str, object]:
    """``payload`` carrying the brief for ``places`` under :data:`BRIEF_KEY`."""
    brief = await brief_for(ctx, home, places)
    return {**payload, BRIEF_KEY: brief} if brief else payload


def _shown(messages: Sequence[ModelMessage], header: str) -> bool:
    return any(
        isinstance(part, ToolReturnPart) and header in str(part.content)
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
    )
