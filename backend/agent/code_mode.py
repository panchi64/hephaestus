"""``run_code``: the model writes a script that calls tools, and only what it returns
reaches the context.

Reading twelve files to find three, or searching per list item, pays for every
intermediate result twice (a round trip, then context the model ignores).
``pydantic_ai_harness``'s ``CodeMode`` folds tools into async Python functions inside a
Monty sandbox that runs in this process. Each call a script makes goes through the agent's
own tool stack (``ToolManager.handle_call``), so the enabled gate, approval gate,
describing stage, narration and capability hooks all apply. The sandbox has no filesystem,
network or clock. Unlike the library default, a folded tool also stays a direct call:
``run_code`` is for fan-outs.

Which tools a script may call is the permission level's answer, read live. A tool is folded
only if it is still an ordinary ``function`` after the approval gate in
``tools/toolsets.py`` marked everything the level doesn't clear, so a tool the level would
ask about stays direct and parks as before. ``CodeMode`` re-reads the catalog each step, so
a mid-conversation level change applies at the next. Three tools stay direct at every
level (:data:`NATIVE_ONLY`): asking a question and entering or submitting a plan, which end
or reshape the turn. A tool whose argument is itself a program (``code_arg_name`` metadata:
``code_execute``, the shell) stays direct by the library's rule.

What must still be ruled on inside a script is the call that gates itself (a global recall,
an untrusted external tool, a skill edit), which the catalog can't foresee.
:class:`CodeModeCapability` rules inline as a top-level batch gets (``agent/gating.py``: the
level, grants, Auto's review, the turn's boundary and review budget); a call needing the
operator is refused with words telling the model to call it directly, where it can park.
A nested call is recognised by context: the sandbox dispatches through its own
``ToolManager`` whose context is the ``run_code`` call's.

The library streams no event for a script's call, so this capability announces each
(``agent/emit.py``'s ``NestedToolStarted`` / ``NestedToolFinished``) and
``agent/translate.py`` turns them into ordinary ``tool.*`` frames carrying the script's
``parent_tool_call_id``. A reload rebuilds the rows from the record the library writes on
the script's result (``services/conversation_view.py``).
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, replace
from typing import cast, override

from pydantic_ai import (
    AbstractToolset,
    DeferredToolRequests,
    DeferredToolResults,
    RunContext,
    ToolApproved,
    ToolDefinition,
    ToolDenied,
)
from pydantic_ai.capabilities.abstract import (
    RawToolArgs,
    ValidatedToolArgs,
    WrapToolExecuteHandler,
)
from pydantic_ai.exceptions import ApprovalRequired, CallDeferred, ModelRetry, ToolFailed
from pydantic_ai.messages import ToolCallPart, ToolReturn
from pydantic_ai.toolsets.abstract import ToolsetTool
from pydantic_ai_harness.code_mode import CodeMode, CodeModeToolset

from core.config import Settings
from core.diagnostics import render_tool_error
from services.permissions import beyond_scope
from services.tool_sensitivity import (
    CHASSIS_TOOLS,
    SENSITIVITY_METADATA_KEY,
    declared_sensitivity,
)
from tools import RunDeps
from tools.code_mode_marks import RUN_CODE, script_call_of
from tools.describe import NARRATION_ARG, NARRATION_PROPERTY, add_property, drop_properties
from tools.recover import describe_failure
from tools.spill import spill_large

from .emit import NestedToolFinished, NestedToolStarted
from .gating import settle_deferred

#: Tools that stay direct at every level. Each ends or reshapes the turn (a question
#: suspends it; a plan changes what the thread may do), which a script can neither wait for
#: nor survive.
NATIVE_ONLY: frozenset[str] = frozenset({"builtin_ask_user", "plan_enter", "plan_submit"})

#: Appended under the signatures, which are bare (``_NarratedCodeModeToolset``).
_CATALOG_NOTE = (
    "\n\nEach function is the tool of the same name, offered to you directly as well; its "
    "description there says what it does. Call a tool directly for one lookup, and write "
    "a script when many calls feed one answer and only the answer needs to come back."
)

# The harness warns once per tool that a folded tool has no return schema (`-> Any`); true of
# most of this catalog and not actionable, so it would be a log warning per tool per process.
warnings.filterwarnings(
    "ignore", message=r"CodeMode: tool .* has no return schema", category=UserWarning
)


@dataclass(frozen=True, slots=True)
class CodeModeLimits:
    """How far one ``run_code`` script may go. The duration and memory caps are Monty's,
    per snippet; ``max_tool_calls`` is how many tools one script may call."""

    max_duration_s: float = 30.0
    max_memory_bytes: int = 256 * 1024 * 1024
    max_tool_calls: int = 25


def code_mode_limits(settings: Settings) -> CodeModeLimits:
    """The limits a turn's scripts run under, from the turn's one settings object."""
    return CodeModeLimits(
        max_duration_s=settings.code_mode_max_duration_s,
        max_memory_bytes=settings.code_mode_max_memory_mb * 1024 * 1024,
        max_tool_calls=max(1, settings.code_mode_max_tool_calls),
    )


def call_directly_message(tool: str) -> str:
    """What a script is told when a call it made needs the operator."""
    return (
        f"`{tool}` needs the operator's approval, which a script cannot wait for. Call "
        f"`{tool}` directly, outside `run_code`, so it can be approved."
    )


def clears_unasked(_ctx: RunContext[RunDeps], tool_def: ToolDefinition) -> bool:
    """Whether a script may call this tool: still an ordinary function after the approval
    gate marked what the level doesn't clear, and not a must-stay-direct tool. The selector
    ``CodeMode`` applies each step."""
    return tool_def.kind == "function" and tool_def.name not in NATIVE_ONLY


def _error_text(name: str, error: Exception) -> str:
    """The text a failed nested call's row shows."""
    if isinstance(error, ModelRetry | ToolFailed):
        return error.message
    return describe_failure(name, error)


@dataclass
class _NarratedCodeModeToolset(CodeModeToolset[RunDeps]):
    """The harness's toolset, with folded tools kept direct and ``run_code`` stated like every
    other tool.

    Adds to the library's definition the ``narration`` argument (``tools/toolsets._narrated``;
    ``run_code`` is added outside that stage) and its sensitivity class
    (``services/tool_sensitivity.py``).
    """

    @staticmethod
    @override
    def _build_description(
        callable_defs: dict[str, ToolDefinition], *, has_os: bool, has_mount: bool
    ) -> str:
        """The library's description, with each function as a bare signature.

        Folded tools are also offered directly with full description and schema, so repeating
        them as docstrings would pay for the catalog twice per request. Only the Python shape
        is rendered (parameters and the return type a script indexes), and the note says
        where the prose is. ``narration`` is omitted: it is the operator's sentence about one
        call, which the script explains itself. (A script passing one isn't refused: the
        type-check stubs keep it and it is stripped before validation.)"""
        compact = {
            name: replace(
                tool_def,
                description=None,
                parameters_json_schema=drop_properties(
                    tool_def.parameters_json_schema, (NARRATION_ARG,)
                ),
            )
            for name, tool_def in callable_defs.items()
        }
        base = CodeModeToolset._build_description(compact, has_os=has_os, has_mount=has_mount)
        return base + _CATALOG_NOTE if compact else base

    @override
    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, object],
        ctx: RunContext[RunDeps],
        tool: ToolsetTool[RunDeps],
    ) -> object:
        """A script's own result is spilled here: it never passes the inner stack's
        recovery layer, which spills every other tool's."""
        result = cast(object, await super().call_tool(name, tool_args, ctx, tool))
        return await spill_large(ctx, name, result) if name == RUN_CODE else result

    @override
    async def get_tools(self, ctx: RunContext[RunDeps]) -> dict[str, ToolsetTool[RunDeps]]:
        tools = await super().get_tools(ctx)
        run_code = tools.pop(RUN_CODE, None)
        if run_code is None:
            return tools
        # The library offers a folded tool only inside `run_code`, which would turn every
        # file read and search into a script; one lookup should be one call. So every tool
        # the stack produced is offered again (the library's `search_tools` note kept) and
        # `run_code` joins at the end. Order is folded tools then the rest, each in stack
        # order (the library returns the sets apart, so interleaving is unrecoverable);
        # stable within a level, though a level change moving a tool across the line moves
        # it in the array.
        wrapped: dict[str, ToolsetTool[RunDeps]] = getattr(run_code, "wrapped_tools", {})
        tools = {name: tools.get(name, tool) for name, tool in wrapped.items()} | {
            name: tool for name, tool in tools.items() if name not in wrapped
        }
        tool_def = run_code.tool_def
        tools[RUN_CODE] = replace(
            run_code,
            tool_def=replace(
                tool_def,
                parameters_json_schema=add_property(
                    tool_def.parameters_json_schema, NARRATION_ARG, NARRATION_PROPERTY
                ),
                metadata={
                    **(tool_def.metadata or {}),
                    SENSITIVITY_METADATA_KEY: CHASSIS_TOOLS[RUN_CODE].value,
                },
            ),
        )
        return tools


@dataclass
class CodeModeCapability(CodeMode[RunDeps]):
    """``CodeMode`` configured for this chassis, plus the rulings and frames nested calls
    need. Every hook acts only on a script's call and passes direct calls through; a
    top-level batch still goes to ``settle_deferred`` and the park."""

    @override
    def get_wrapper_toolset(
        self, toolset: AbstractToolset[RunDeps]
    ) -> AbstractToolset[RunDeps] | None:
        # Non-eager tier, no host access, static catalog: `eager` runs a statement before
        # any hook sees the finished call, a mount or OS handler hands the sandbox the host,
        # and a dynamic catalog adds an instruction part that moves with it.
        return _NarratedCodeModeToolset(
            wrapped=toolset,
            tool_selector=self.tools,
            max_retries=self.max_retries,
            max_tool_calls=self.max_tool_calls,
            resource_limits=self.resource_limits,
            capability=self,
        )

    @override
    async def before_tool_validate(
        self,
        ctx: RunContext[RunDeps],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: RawToolArgs,
    ) -> RawToolArgs:
        """Announce a script's call as it begins, skipping an approved call's re-validation."""
        parent = script_call_of(ctx)
        if parent is not None and not ctx.tool_call_approved:
            _ = await ctx.emit(
                NestedToolStarted(
                    parent_tool_call_id=parent,
                    part=call,
                    tool_call_id=call.tool_call_id,
                    tool_name=call.tool_name,
                )
            )
        return args

    @override
    async def on_tool_validate_error(
        self,
        ctx: RunContext[RunDeps],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: RawToolArgs,
        error: Exception,
    ) -> ValidatedToolArgs:
        parent = script_call_of(ctx)
        if parent is not None:
            text = (
                error.message
                if isinstance(error, ModelRetry | ToolFailed)
                else render_tool_error(f"the arguments to {call.tool_name} are invalid: {error}")
            )
            await self._finished(ctx, parent, call, error=text)
        raise error

    @override
    async def before_tool_execute(
        self,
        ctx: RunContext[RunDeps],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: ValidatedToolArgs,
    ) -> ValidatedToolArgs:
        """Send a script's call the level no longer clears back to be ruled on.

        The catalog was read when the step began; a level that moved since (a plan entered in
        the same batch) would let the script call what is now withheld or asked about.
        Raised as a deferral so the handler below gives the ruling a direct call would get
        under the live level: withheld at Plan, reviewed at Auto, refused with the way round
        where only the operator could answer."""
        if (
            script_call_of(ctx) is not None
            and not ctx.tool_call_approved
            and beyond_scope(
                ctx.deps.permission,
                tool_def.name,
                declared=declared_sensitivity(tool_def.metadata),
            )
        ):
            raise ApprovalRequired()
        return args

    @override
    async def wrap_tool_execute(
        self,
        ctx: RunContext[RunDeps],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: ValidatedToolArgs,
        handler: WrapToolExecuteHandler,
    ) -> object:
        """Announce how a script's call ended. A deferral is not an ending: the handler rules
        on it and an approved call returns here to run."""
        parent = script_call_of(ctx)
        if parent is None:
            return cast(object, await handler(args))
        try:
            result = cast(object, await handler(args))
        except ApprovalRequired, CallDeferred:
            raise
        except Exception as exc:
            await self._finished(ctx, parent, call, error=_error_text(call.tool_name, exc))
            raise
        outcome: object = result
        if isinstance(outcome, ToolReturn):
            returned = cast(ToolReturn[object], outcome)
            await self._finished(
                ctx, parent, call, content=returned.return_value, user_content=returned.content
            )
        else:
            await self._finished(ctx, parent, call, content=result)
        return result

    @override
    async def handle_deferred_tool_calls(
        self,
        ctx: RunContext[RunDeps],
        *,
        requests: DeferredToolRequests,
    ) -> DeferredToolResults | None:
        """Rule inline on a script's call that gated itself, as a top-level batch is ruled,
        minus the outcome a script can't take: waiting for the operator.

        The context here is the sandbox manager's, i.e. the ``run_code`` call's, so a nested
        request names that tool; a top-level batch names none and is declined, left to
        ``settle_deferred`` and the park."""
        if ctx.tool_name != RUN_CODE:
            return None
        parent = ctx.tool_call_id or ""
        deps = ctx.deps
        settled: dict[str, ToolApproved | ToolDenied] = {}
        manual: list[ToolCallPart] = []
        if requests.approvals:
            settled, manual = await settle_deferred(
                deps.run,
                requests.approvals,
                caps=deps.caps,
                conversation_id=deps.conversation_id,
                deps=deps,
                messages=list(ctx.messages),
                permission=deps.permission,
                turn_start=deps.turn_start,
                budget=deps.review_budget,
                metadata=requests.metadata,
            )
        needs_operator = {call.tool_call_id for call in manual}
        approvals: dict[str, ToolApproved | ToolDenied | bool] = {}
        for call in requests.approvals:
            decision = (
                ToolDenied(message=call_directly_message(call.tool_name))
                if call.tool_call_id in needs_operator
                else settled[call.tool_call_id]
            )
            approvals[call.tool_call_id] = decision
            if isinstance(decision, ToolDenied):
                await self._finished(ctx, parent, call, error=decision.message)
        # A call that deferred itself for someone else to run — nobody inside a script can.
        calls: dict[str, ToolFailed] = {}
        for call in requests.calls:
            message = call_directly_message(call.tool_name)
            calls[call.tool_call_id] = ToolFailed(message)
            await self._finished(ctx, parent, call, error=message)
        return DeferredToolResults(approvals=approvals, calls=calls)

    async def _finished(
        self,
        ctx: RunContext[RunDeps],
        parent: str,
        call: ToolCallPart,
        *,
        content: object = None,
        user_content: object = None,
        error: str | None = None,
    ) -> None:
        _ = await ctx.emit(
            NestedToolFinished(
                parent_tool_call_id=parent,
                content=content,
                user_content=user_content,
                error=error,
                tool_call_id=call.tool_call_id,
                tool_name=call.tool_name,
            )
        )


def code_mode_capability(limits: CodeModeLimits | None = None) -> CodeModeCapability:
    """The ``run_code`` capability every agent carries, under ``limits``."""
    limits = limits or CodeModeLimits()
    return CodeModeCapability(
        tools=clears_unasked,
        max_tool_calls=limits.max_tool_calls,
        resource_limits={
            "max_duration_secs": limits.max_duration_s,
            "max_memory": limits.max_memory_bytes,
        },
    )
