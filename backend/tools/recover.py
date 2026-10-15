"""The outermost toolset: an unexpected exception from a tool becomes a failure the model
reads and works around, instead of an error that ends the run.

Pydantic AI recovers only the exceptions it defines for the purpose. Anything else a tool
raises leaves ``agent.iter`` and stops the turn, losing the work in progress over what is
often one bad file path or one flaky service. Here such an exception is logged and
re-raised as ``ToolFailed``, which reports the failure to the model without spending the
tool's retry budget.

Each call also holds the run's keepalive. A tool that legitimately works for minutes
without streaming (a build, a VM bring-up) would otherwise look idle to the inactivity
watchdog; the per-call timeout bounds it instead.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import cast, override

from pydantic import JsonValue
from pydantic_ai import ApprovalRequired, CallDeferred, ModelRetry, RunContext, ToolFailed
from pydantic_ai.exceptions import (
    AgentRunError,
    SkipToolExecution,
    SkipToolValidation,
    ToolFailedError,
    ToolRetryError,
    UserError,
)
from pydantic_ai.toolsets import ToolsetTool, WrapperToolset

from core.diagnostics import PLATFORM_CODE, render_tool_error
from core.exceptions import HephaestusError

from .deps import RunDeps

logger = logging.getLogger(__name__)

#: Raised on purpose to steer the run: deferrals, retries, terminal failures already
#: worded for the model, and the library's own run-level stops. ``UserError`` is the
#: library refusing how it was wired, a bug that would fail every call alike, so it stays
#: fatal rather than becoming something the model is asked to work around.
_PASS_THROUGH = (
    ApprovalRequired,
    CallDeferred,
    ModelRetry,
    ToolFailed,
    ToolFailedError,
    ToolRetryError,
    SkipToolExecution,
    SkipToolValidation,
    AgentRunError,
    UserError,
)


def describe_failure(name: str, error: Exception) -> str:
    """The model-facing text for an exception a tool did not handle itself.

    A domain error is the environment's (a VM that is down, a service that is degraded)
    and carries its own hint. Anything else is a fault in the platform, marked as such so
    the operator sees it and the model does not keep retrying the same call.
    """
    if isinstance(error, HephaestusError):
        return render_tool_error(
            f"{name} could not finish: {error}",
            hints=[error.hint] if error.hint else ["try another way to reach the same goal"],
        )
    return render_tool_error(
        f"{name} failed unexpectedly: {type(error).__name__}: {error}",
        cause="a fault in the platform, not in the arguments you passed",
        hints=[
            "try another tool or approach for this step",
            "if this step cannot be done another way, tell the operator what failed",
        ],
        code=PLATFORM_CODE,
    )


@dataclass
class RecoveringToolset(WrapperToolset[RunDeps]):
    #: Upper bound on one call, keepalive included; None leaves calls unbounded.
    call_timeout_s: float | None = None

    @override
    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, JsonValue],
        ctx: RunContext[RunDeps],
        tool: ToolsetTool[RunDeps],
    ) -> object:
        deadline = asyncio.timeout(self.call_timeout_s)
        try:
            async with ctx.deps.run.keepalive(), deadline:
                return cast(object, await self.wrapped.call_tool(name, tool_args, ctx, tool))
        except _PASS_THROUGH:
            raise
        except Exception as exc:
            if isinstance(exc, TimeoutError) and deadline.expired():
                raise ToolFailed(
                    render_tool_error(
                        f"{name} was stopped after {self.call_timeout_s or 0:.0f}s unfinished",
                        hints=["split the work into smaller calls, or run it in the background"],
                    )
                ) from exc
            if isinstance(exc, HephaestusError):
                logger.warning("tool %s failed: %s", name, exc)
            else:
                logger.exception("tool %s raised", name)
            raise ToolFailed(describe_failure(name, exc)) from exc
