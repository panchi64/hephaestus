"""Built-in utility tools — the minimal starter category.

Real capabilities (memory, web, email, shell, …) arrive as their services land;
each becomes a thin tool over a ``services/`` capability. This category exists
so the toolset stack has something to compose and gate today.

``ask_user`` is the exception to that shape: it is backed by no service at all,
because the thing it reaches for is the operator. It **defers** rather than
returning — the call parks the turn, the operator answers in the interface, and
the answer arrives as this tool's return value on the resume.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field
from pydantic_ai import FunctionToolset, RunContext
from pydantic_ai.exceptions import CallDeferred

from runs.lanes import is_attended

from .deps import RunDeps

#: What a turn that asks in a run nobody is watching gets back instead of parking
#: forever. Belt-and-braces: `services/tool_policy.lane_disabled_tools` withholds the
#: tool from those runs entirely, so this should be unreachable — but "unreachable" and
#: "hangs the run until the process restarts" are too far apart to leave to one gate.
NO_OPERATOR = (
    "No operator is available to answer in this run (it is running unattended). "
    "Decide with your best judgment and continue."
)


class AskOption(BaseModel):
    """One offered answer."""

    label: str = Field(description="The answer itself, as the operator will see it.")
    description: str | None = Field(
        default=None,
        description="One short line on what choosing this would mean. Omit when the label says it.",
    )


class Question(BaseModel):
    """One question, with the answers offered for it."""

    question: str = Field(description="The question, in plain language.")
    options: list[AskOption] = Field(min_length=2, max_length=4)
    multi_select: bool = Field(
        default=False,
        description="True when several options may be chosen together.",
    )


def builtin_toolset() -> FunctionToolset[RunDeps]:
    toolset: FunctionToolset[RunDeps] = FunctionToolset()

    @toolset.tool_plain
    def now() -> str:
        """Return the current date and time on the operator's own clock, ISO 8601 with its
        offset from UTC (e.g. 2026-03-14T09:41:07+01:00)."""
        return datetime.now().astimezone().isoformat()

    @toolset.tool
    def ask_user(
        ctx: RunContext[RunDeps],
        questions: Annotated[list[Question], Field(min_length=1, max_length=4)],
    ) -> str:
        """Ask the operator to decide something, and wait for their answer.

        The turn pauses here and resumes with their reply, so nothing you have worked out
        so far is lost — this costs far less than guessing and being wrong.

        The operator can always write an answer of their own instead of choosing one of
        your options, so offer the choices you think most likely rather than trying to
        cover every case.

        Ask only what only they can answer: a preference, a tradeoff, a decision that
        turns on what they want. Anything the code, the files or the tools can tell you,
        find out yourself.

        Ask everything you need in ONE call — up to four questions, answered together.
        """
        # Guard before deferring: an unattended run has nobody to park on.
        if not is_attended(ctx.deps.run.kind):
            return NO_OPERATOR
        raise CallDeferred

    return toolset
