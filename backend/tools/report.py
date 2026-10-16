"""The `subagent` category: the one tool a spawned agent ends its work with.

A spawned agent's report is the only thing that survives it, so it travels as a tool call
with a status rather than as whatever the agent happened to say last. Its parent learns
whether the work is done, waiting on an answer, or given up, without reading it out of
prose. Offered only in a thread another agent spawned (`toolsets._enabled_gate`), and kept
out of the dormant `agents` category, since an agent that had to search for the way to
finish would sometimes not find it.
"""

from __future__ import annotations

from typing import Literal

from pydantic_ai import AbstractToolset, FunctionToolset, ModelRetry, RunContext

from core.diagnostics import render_tool_error
from runs.lanes import is_attended
from services.subagents import FiledReport, SubagentLauncher, SubagentUnavailableError

from .deps import RunDeps

#: The tool's namespaced name, which the enabled gate offers to spawned threads alone.
REPORT_TOOL = "subagent_report"


def report_toolset() -> AbstractToolset[RunDeps]:
    toolset = FunctionToolset[RunDeps]()

    @toolset.tool
    async def report(
        ctx: RunContext[RunDeps],
        status: Literal["done", "blocked", "failed"],
        report: str,
    ) -> dict[str, object]:
        """Hand your report to the agent that spawned you. This is your last action: call
        it once, when the work is over, then end your turn without writing anything else.

        `status` is `done` when the task is finished, `blocked` when you need an answer or
        a decision before you can go on (your parent's reply wakes you where you stopped),
        and `failed` when you tried the safe routes and the task cannot be done as briefed.

        `report` is everything your parent needs, because nothing else from this thread
        reaches it: the result first, then how you checked it, what you could not do, and
        anything it would be wrong to assume. For `blocked`, end with the exact question.
        For `failed`, say what broke and what you tried.
        """
        text = report.strip()
        if not text:
            raise ModelRetry(
                render_tool_error(
                    "the report is empty",
                    hints=["write what you found or did; your parent sees nothing else"],
                )
            )
        launcher = ctx.deps.caps.get_optional(SubagentLauncher)
        if launcher is None:
            return {"filed": False, "detail": "Delegation is unavailable in this deployment."}
        try:
            parent = await launcher.file_report(
                ctx.deps.owner_id,
                ctx.deps.conversation_id or "",
                ctx.deps.run.id,
                FiledReport(status=status, text=text),
            )
        except SubagentUnavailableError as exc:
            return {"filed": False, "detail": str(exc)}
        ctx.deps.reported = True
        return {
            "filed": True,
            "to": parent,
            "detail": "It reaches them when this turn ends. End your turn now, with no "
            + "further text: nobody reads this thread.",
        }

    return toolset


async def require_report[OutputT](ctx: RunContext[RunDeps], output: OutputT) -> OutputT:
    """Send a spawned agent's final answer back, once a turn, when it has not reported.

    Only an unattended turn is held to it: when the operator is typing in the agent's
    thread, they are the one reading its answers. A second answer without a report stands,
    and the parent is told the agent ended without reporting.
    """
    deps = ctx.deps
    if (
        not isinstance(output, str)
        or not deps.reports_to_parent
        or deps.reported
        or deps.report_reminded
        or is_attended(deps.run.kind)
    ):
        return output
    deps.report_reminded = True
    raise ModelRetry(
        render_tool_error(
            f"you ended your turn without calling `{REPORT_TOOL}`",
            cause="your parent only receives what that tool carries",
            hints=[
                f"call `{REPORT_TOOL}` with `done`, `blocked` or `failed` and your full "
                + "report, then end your turn",
            ],
        )
    )
