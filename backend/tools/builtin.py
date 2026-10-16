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

    label: str = Field(
        description="The answer itself, a few words the operator can pick at a glance."
    )
    description: str | None = Field(
        default=None,
        description=(
            "One short line on what choosing this means: the consequence or the tradeoff. "
            "On the option you recommend, say that it is your recommendation and why. "
            "Omit when the label says it all."
        ),
    )


class Question(BaseModel):
    """One question, with the answers offered for it."""

    question: str = Field(
        description=(
            "The question in plain language, ending in a question mark. Give the context "
            "the operator needs to answer it, since they may not have followed your work."
        )
    )
    options: list[AskOption] = Field(
        min_length=2,
        max_length=4,
        description=(
            "Two to four concrete answers, the one you recommend first. Leave out an "
            '"Other" option: the operator can always write their own answer.'
        ),
    )
    multi_select: bool = Field(
        default=False,
        description="True when several options can be chosen together, not as alternatives.",
    )


def builtin_toolset() -> FunctionToolset[RunDeps]:
    toolset: FunctionToolset[RunDeps] = FunctionToolset()

    @toolset.tool_plain
    def now() -> str:
        """Returns the current date and time on the operator's clock, in ISO 8601 with the
        offset from UTC (`2026-03-14T09:41:07+01:00`).

        Your context carries the date but not the time, so call this when the time of
        day matters: a deadline today, "this morning", how long ago something happened,
        or a timestamp to pass to another tool.
        """
        return datetime.now().astimezone().isoformat()

    @toolset.tool
    def ask_user(
        ctx: RunContext[RunDeps],
        questions: Annotated[list[Question], Field(min_length=1, max_length=4)],
    ) -> str:
        """Asks the operator one to four questions as a card they answer by clicking, and
        pauses the turn until they do. The turn resumes with their answers, and nothing
        you have worked out so far is lost.

        Use it when the answer changes what you do next and only the operator can give
        it: a preference, a tradeoff, a reading of an ambiguous request, a choice between
        approaches that are each valid. Asking costs far less than guessing wrong and
        redoing the work. Never ask what the code, the files or your tools can tell you;
        find that out yourself first, so the question you ask is the one that is left.

        Put every open decision into one call, up to four questions answered together,
        rather than asking one per turn. Ask through this tool rather than in your reply,
        because a question written in prose ends your turn with nothing for the operator
        to click. Don't repeat in text what the card already asks.

        Give each question two to four concrete options, the one you recommend first,
        with a one-line `description` where the consequence is not obvious from the
        label. The operator can always write their own answer, so offer the likeliest
        choices rather than trying to cover every case.

        Returns each question with the option or options chosen and anything the operator
        wrote. In a run nobody is watching, it returns at once and tells you to decide
        for yourself.
        """
        del questions  # Carried to the operator by the deferral, not read by this body.
        # Guard before deferring: an unattended run has nobody to park on.
        if not is_attended(ctx.deps.run.kind):
            return NO_OPERATOR
        raise CallDeferred

    return toolset
