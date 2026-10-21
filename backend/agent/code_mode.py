"""``run_code`` — the model writes a script that calls tools, and only what the script
returns reaches its context.

A turn that has to read twelve files to find the three that mention something, or run a
search per item in a list, pays for every intermediate result twice: once as a round trip
and once as context the model reads and then ignores. ``pydantic_ai_harness``'s
``CodeMode`` removes both: the tools it folds become async Python functions inside a
Monty sandbox that runs **in this process**, and each call the script makes goes through
the agent's own tool stack (``ToolManager.handle_call``) — the enabled gate, the approval
gate, the describing stage, narration and every capability hook still apply to it. The
sandbox itself has no filesystem, no network and no clock; everything a script does, it
does by calling a tool. Unlike the library's default, a folded tool **stays a direct call
as well**: ``run_code`` is for a fan-out, and a single lookup is still a single call.

**Which tools a script may call is the permission level's answer, read live.** A tool is
folded only when it is still an ordinary ``function`` after ``tools/toolsets.py``'s
approval gate has marked everything the thread's level does not clear — so a tool the
level would ask about stays a direct call and parks exactly as it did before, and a
script never holds a call nobody can answer. ``CodeMode`` re-reads the catalog every step,
so a level that moves mid-conversation moves the catalog with it at the next step. Three
tools stay direct at every level (:data:`NATIVE_ONLY`): asking the operator a question,
and entering or submitting a plan — each of which ends or reshapes the turn, which a
script cannot wait on. A tool whose argument is itself a program (``code_arg_name``
metadata — ``code_execute``, the shell) stays direct by the library's own rule.

**What still has to be ruled on inside a script** is the call that gates *itself* — a
global recall, an untrusted external tool, a skill edit — which the catalog cannot see
coming. :class:`CodeModeCapability` answers those inline with the same ruling a top-level
batch gets (``agent/gating.py`` — the level, the operator's grants, Auto's review, with
the turn's own boundary and review budget); a call that would need the operator is
refused with words telling the model to call the tool directly, where it can park. A
nested call is recognised by the context it runs in: the sandbox dispatches through a
``ToolManager`` of its own whose context is the ``run_code`` call's.

**And what the operator sees.** The library streams no event for a call a script makes,
so this capability announces each one (``agent/emit.py``'s ``NestedToolStarted`` /
``NestedToolFinished``) and ``agent/translate.py`` turns them into the ordinary
``tool.*`` frames, carrying the script's ``parent_tool_call_id``. A reload rebuilds the
same rows from the record the library writes on the script's own result
(``services/conversation_view.py``).
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

#: Tools that stay direct calls at every level, whatever the level clears. Each one ends
#: or reshapes the turn — a question suspends it for the operator, and entering or
#: submitting a plan changes what the thread may do — and a script can neither wait for
#: an answer nor carry on under a level that moved beneath it.
NATIVE_ONLY: frozenset[str] = frozenset({"builtin_ask_user", "plan_enter", "plan_submit"})

#: Appended under the signatures, which are bare (``_NarratedCodeModeToolset``).
_CATALOG_NOTE = (
    "\n\nEach function is the tool of the same name, offered to you directly as well; its "
    "description there says what it does. Call a tool directly for one lookup, and write "
    "a script when many calls feed one answer and only the answer needs to come back."
)

# The harness warns once per tool that a folded tool has no return schema, so its
# signature reads `-> Any`. That is true of most of this catalog and is not something an
# operator can act on; left alone it is a warning per tool per process in the log.
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
    """Whether a script may call this tool: it is still an ordinary function after the
    approval gate marked everything this run's level does not clear, and it is not one of
    the tools that must stay direct. The selector ``CodeMode`` applies each step."""
    return tool_def.kind == "function" and tool_def.name not in NATIVE_ONLY


def _error_text(name: str, error: Exception) -> str:
    """The text a failed nested call's row shows."""
    if isinstance(error, ModelRetry | ToolFailed):
        return error.message
    return describe_failure(name, error)


@dataclass
class _NarratedCodeModeToolset(CodeModeToolset[RunDeps]):
    """The harness's toolset, with the folded tools kept direct and ``run_code`` stated
    the way every other tool is.

    Two additions to the one definition the library builds: the ``narration`` argument
    every tool in this catalog is offered (``tools/toolsets._narrated`` — ``run_code`` never
    passes through that stage, since it is added outside it), and its sensitivity class,
    so a reader of the definition gets the answer ``services/tool_sensitivity.py`` gives.
    """

    @staticmethod
    @override
    def _build_description(
        callable_defs: dict[str, ToolDefinition], *, has_os: bool, has_mount: bool
    ) -> str:
        """The library's description, with each function stated as a bare signature.

        Every folded tool is also offered directly (below), with its whole description
        and schema, so repeating each one as a docstring here would pay for the catalog
        twice on every request. What a script needs that the direct definition does not
        give is the Python shape — the parameters, and the return type a script indexes
        into — so that is what is rendered, and the note says where the prose is.
        ``narration`` is left out of the signatures too: it is the operator's sentence
        about one call, and a script's calls are explained by the script's own. (A script
        that passes one anyway is not refused — the type-check stubs keep it, and it comes
        off before validation like any other call's.)"""
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
        # A folded tool stays a direct call too. The library's split is exclusive — a tool
        # it folds is offered *only* as a function inside `run_code` — which would turn
        # every file read and every search into a script. `run_code` is the tool for a
        # fan-out whose intermediate results the model does not need to see; one lookup is
        # still one call. So every tool the stack produced is offered again (the library's
        # `search_tools` note kept) and `run_code` joins at the end. The order is the folded
        # tools first, then the rest, each in the stack's order — the library hands the two
        # sets back apart, so the interleaving is not recoverable. It is stable within a
        # level; a level change that moves a tool across the line moves it in the array.
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
    """``CodeMode`` configured for this chassis, plus the rulings and frames its nested
    calls need. Every hook below acts only on a call a script made and passes a direct
    call through untouched — a top-level batch goes to ``settle_deferred`` and the park
    exactly as it did before this capability existed."""

    @override
    def get_wrapper_toolset(
        self, toolset: AbstractToolset[RunDeps]
    ) -> AbstractToolset[RunDeps] | None:
        # Only the non-eager tier, with no host access and a static catalog: `eager` would
        # run a statement before any hook saw the finished call, a mount or an OS handler
        # would hand the sandbox the host, and a dynamic catalog adds an instruction part
        # that moves every time the catalog does.
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
        """Announce a script's call as it begins. Skipped on the re-validation an approved
        call goes through, which is the same call already announced."""
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
        """Send a script's call the thread's level no longer clears back to be ruled on.

        The script's catalog was read when the step began; a level that moved since (a
        plan entered beside the script in the same batch) would otherwise let it call
        what the level now withholds or asks about. Raised as a deferral, so the handler
        below gives it the ruling the same call made directly would get under the live
        level — withheld at Plan, reviewed at Auto, refused with the way round where only
        the operator could answer."""
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
        """Announce how a script's call ended. A deferral is not an ending — the handler
        rules on it, and an approved call comes back through here to run."""
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
        """Rule on a script's call that gated itself, inline — the same ruling a top-level
        batch gets, minus the one outcome a script cannot take: waiting for the operator.

        The context here is the sandbox manager's own, which is the ``run_code`` call's,
        so a nested request is one whose context names that tool; a top-level batch's
        names none and is declined, leaving it to ``settle_deferred`` and the park."""
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
