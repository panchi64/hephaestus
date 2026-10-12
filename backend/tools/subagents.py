"""The `subagents` category — handing a self-contained piece of work to another agent.

Four tools, and the shape of them is the design. One hands work over; the other three exist
because handing it over is not the same as forgetting about it — `send` changes what a
sub-agent was asked for while it still can, `list` says what is still out, and `read` says
where one has got to.

**`launch` does not wait.** A sub-agent runs as its own Run on the same substrate and
takes minutes. Blocking this turn on it would spend the turn's whole step budget watching
a progress bar — which is exactly what the delegation this replaced did, and the reason a
five-minute sub-agent used to mean a five-minute silence. So `launch` returns the moment
the run is submitted, and says so in words: the model can launch several in one step,
carry on, and end its turn. It is told what each one found as each one finishes.

**The roster is generated, not written.** The one place the model reads what it may launch
is this tool's description, because that is where it is deciding — a standing instruction
at the prompt head would say the same thing a second time inside the cached prefix, and
say it furthest from the choice. Generating it from the roster is what stops the classic
failure: a hand-written list naming sub-agents that no longer exist and omitting the ones
that do, with nothing to make the two disagree loudly.

**And regenerated per run**, because a project declares sub-agents of its own in files and
which project that is depends on the run. The description is rewritten in `get_tools` from
the run's own roster, so a repository's `reviewer` is one the model can actually see rather
than one it would have to be told about separately — and one the model *could* not see is
one that may as well not exist.

**Only a sub-agent that can write is gated.** Reading changes nothing, and asking about it
would be a question with one sensible answer asked over and over. One that edits does work
the operator's own merge will carry out, so launching it is theirs to allow — once per
conversation, if they grant it that far.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from pydantic_ai import AbstractToolset, FunctionToolset, ModelRetry, RunContext
from pydantic_ai.exceptions import ApprovalRequired
from pydantic_ai.toolsets import ToolsetTool, WrapperToolset

from services.permissions import STRICTEST_PERMISSION
from services.subagents import (
    AgentNameError,
    SubagentLauncher,
    SubagentParent,
    SubagentSpec,
    SubagentUnavailableError,
    builtin_roster,
    describe_roster,
    normalize_agent_name,
)
from tools.deps import RunDeps
from tools.project_agents import run_roster

#: The launch tool's name inside the toolset, before the category prefix makes it
#: ``subagents_launch``. Named rather than spelled at each of its two uses — the
#: registration and the per-run description rewrite — because those two agreeing is what
#: decides whether the rewrite lands on anything at all, and a typo would simply do nothing.
LAUNCH_TOOL = "launch"

#: The conditionally-gated name this category contributes to the approval-scope
#: vocabulary. The raise below parks the run either way, but a name absent from the
#: assembled gated set never reaches the approval scopes, so the operator could not grant
#: it for the conversation and would be asked once per launch.
GATED_TOOLS: frozenset[str] = frozenset({f"subagents_{LAUNCH_TOOL}"})

_UNAVAILABLE = "Sub-agents are unavailable in this deployment."

_LAUNCH_DOC = """Hand a self-contained piece of work to a sub-agent, which does it on its own.

{roster}

It returns **immediately** and does not wait; never poll for one in a loop. Launch as many
as the work genuinely splits into, issuing the calls together, then carry on with whatever
does not depend on them and end your turn when nothing is left. **You will be told what
each found as it finishes.** Some may queue rather than start at once — that is throughput,
not failure: every one you launched will run and will report.

Launch one when a piece of work needs a lot of reading or editing and the steps themselves
are not what the operator wants to see. Not for work you could do in a couple of tool
calls: the round trip costs more than it saves.

`name` is what *you* are calling this one: a short hyphenated handle describing the job it
is for — `helper-function-finder`, `test-runner`, `auth-route-reviewer` — not a repeat of
the agent you picked. It has to be unique within this thread, and it is the only way to
address this sub-agent afterwards, so name it for the piece of work rather than for the
worker: three `explorer`s are indistinguishable, three jobs are not.

`task` has to stand alone — the sub-agent starts from an empty history and never sees this
conversation. Say what to do, where to start, and what a finished answer looks like; "the
bug we discussed" describes nothing it can act on.

`isolate` gives it its own copy of the workspace, merged back when it finishes, instead of
your files. Ask for it when you mean to keep editing meanwhile: two agents in one tree is
fine when one is waiting and a mess when neither is. A file you both changed comes back as
a reported conflict rather than silently taking one side.

Once one is running you are not stuck with what you asked for: `subagents_send` amends a
sub-agent's brief while it works, and `subagents_list` says which of yours are still out.
"""


def subagents_toolset() -> AbstractToolset[RunDeps]:
    toolset = FunctionToolset[RunDeps]()

    async def launch(
        ctx: RunContext[RunDeps],
        name: str,
        agent_name: str,
        task: str,
        isolate: bool = False,
    ) -> dict:
        launcher = ctx.deps.caps.get_optional(SubagentLauncher)
        if launcher is None:
            return {"launched": False, "detail": _UNAVAILABLE}
        try:
            # Validated here as well as normalised at the seam, because *this* is where
            # the model can be told what it got wrong while the launch is still worth
            # making. The rule itself is not restated — it is the one the roster's own
            # names are held to.
            name = normalize_agent_name(name)
        except AgentNameError as exc:
            raise ModelRetry(
                f"{exc} Name it for the job this sub-agent is for, like `helper-function-finder`."
            ) from exc
        roster = await run_roster(ctx)
        spec = roster.get(agent_name)
        if spec is None:
            # Recoverable: the model very likely guessed a name. Naming the roster back
            # costs one retry, where failing the turn costs the whole thing.
            raise ModelRetry(
                f"There is no sub-agent called {agent_name!r}. Choose one of: "
                f"{', '.join(sorted(roster))}."
            )
        # Before anything else, and only for one that can change things: a launch that
        # ends in edits is the operator's to allow, and asking after a workspace has been
        # forked for it would be a side effect they never approved.
        if _can_write(spec) and not ctx.tool_call_approved:
            raise ApprovalRequired()
        try:
            started = await launcher.launch(
                ctx.deps.owner_id,
                spec,
                task,
                handle=name,
                parent=_parent(ctx),
                isolate=isolate,
            )
        except SubagentUnavailableError as exc:
            # A state the system is in, not a bug — hand it back so the model can adapt
            # (do the work itself, or tell the operator what to switch on).
            return {"launched": False, "detail": str(exc)}
        # `started.handle`, never the `name` that was asked for: handles are unique within
        # the thread, so a second `explorer` came back as `explorer-2` and echoing the
        # request would hand the model a name that addresses the first one.
        return {
            "launched": True,
            "name": started.handle,
            "agent_name": started.name,
            "detail": (
                "Running on its own. Do not wait for it and do not poll — you will be "
                "told what it found when it finishes. Carry on, or end your turn."
                + (
                    ""
                    if started.handle == name
                    else f" Named `{started.handle}`, because `{name}` was already taken "
                    "in this thread — address it by that."
                )
            ),
        }

    # The built-ins alone, which is what the operator's catalog shows and what a run with
    # no project of its own is offered. A run that has one gets this same text with its
    # roster in it, rewritten in `get_tools` below.
    launch.__doc__ = launch_description(builtin_roster())
    # Deliberately *not* `requires_approval=True`. That marking defers the call before the
    # body runs, which would park every launch — including an explorer that can only read —
    # and leave the conditional gate below deciding nothing. Raising from inside is what
    # makes "only a sub-agent that can write is gated" true, and it is also what lets a
    # guessed agent name come back as one retry instead of as a question the operator has
    # to answer before the model can be told it got the name wrong.
    toolset.add_function(launch, name=LAUNCH_TOOL)

    @toolset.tool(name="send")
    async def send_to_subagent(ctx: RunContext[RunDeps], name: str, message: str) -> dict:
        """Redirect a sub-agent that is still working, by amending what you asked it for.

        `name` is what you called it when you launched it.

        Use it when what you want from one has genuinely changed — a constraint arrived, the
        operator narrowed the question, another sub-agent already covered half of it. It is
        far cheaper than letting one finish the wrong work and launching a replacement.

        This is **not** a conversation. The sub-agent does not answer you; it folds your
        message into its brief and carries on, and the one thing you get back from it is its
        report when it finishes. Do not use this to ask it how it is getting on — that is
        `subagents_read`.

        Delivery is best effort: the sub-agent reads it at its next step, so one that is
        about to finish may never see it. The result says which.
        """
        launcher = ctx.deps.caps.get_optional(SubagentLauncher)
        if launcher is None:
            return {"sent": False, "detail": _UNAVAILABLE}
        try:
            view = await launcher.steer(
                ctx.deps.owner_id, ctx.deps.conversation_id or "", name, message
            )
        except SubagentUnavailableError as exc:
            # Returned rather than raised as a retry, the way `launch` answers the same
            # exception. Every state it reports is settled — the sub-agent finished, or
            # this thread has nothing by that name — so a retry of this call cannot come
            # out differently and would spend a round trip proving it. The detail says what
            # to do instead.
            return {"sent": False, "detail": str(exc)}
        return {
            "sent": True,
            "name": view.handle,
            "agent_name": view.name,
            "status": view.status,
            "detail": (
                "Queued for the sub-agent's next step. It will not reply to this — carry "
                "on, and you will get its report when it finishes. If it was already "
                "close to done it may finish without reading this, so do not assume the "
                "report reflects it."
            ),
        }

    @toolset.tool(name="list")
    async def list_subagents(ctx: RunContext[RunDeps]) -> dict:
        """The sub-agents of this thread that are still working — what you are waiting on.

        One that has finished is not here: it has already told you what it found, so this is
        the list of what is still outstanding rather than a history to compare against.

        Check it before you conclude. Answering while half the work is still out produces an
        answer built on half the evidence, and you will be woken by the rest of it
        afterwards with nothing left to do about it.
        """
        launcher = ctx.deps.caps.get_optional(SubagentLauncher)
        if launcher is None:
            return {"available": False, "detail": _UNAVAILABLE}
        # `or ""` rather than the bare value: `live` reads `None` as "every thread's", and
        # a run with no conversation of its own (a scheduled task) would be handed every
        # live sub-agent the operator has — along with the handles to redirect them with,
        # which is the sibling-injection hole `_NO_RECURSION` closes on the other axis. A
        # thread-less run's own launches are recorded under `""`, so this scopes it to
        # exactly what it launched.
        views = await launcher.live(
            ctx.deps.owner_id, conversation_id=ctx.deps.conversation_id or ""
        )
        return {
            "available": True,
            "working": [
                {
                    "name": view.handle,
                    "agent_name": view.name,
                    "task": view.task,
                    "status": view.status,
                }
                for view in views
            ],
            "detail": (
                "Nothing of yours is still working — everything you launched has reported."
                if not views
                else "Still working. You will be told as each one finishes; do not poll."
            ),
        }

    @toolset.tool(name="read")
    async def read_subagent(ctx: RunContext[RunDeps], name: str) -> dict:
        """Check on a sub-agent you launched — whether it is still going, and what it has
        said so far.

        `name` is what you called it when you launched it.

        You do not need this to receive a sub-agent's report: a finished sub-agent tells
        you itself. Use it when you need to know *now* whether one is still working — a
        `running` status means it is, and calling this again in a loop will not make it
        finish sooner.
        """
        launcher = ctx.deps.caps.get_optional(SubagentLauncher)
        if launcher is None:
            return {"available": False, "detail": _UNAVAILABLE}
        try:
            view = await launcher.read(ctx.deps.owner_id, ctx.deps.conversation_id or "", name)
        except SubagentUnavailableError as exc:
            raise ModelRetry(str(exc)) from exc
        return {
            "available": True,
            "name": view.handle,
            "agent_name": view.name,
            "status": view.status,
            "report": view.summary,
            "error": view.error,
        }

    return _ProjectRoster(toolset)


def launch_description(roster: Mapping[str, SubagentSpec]) -> str:
    """What the model is told it may launch, for one roster."""
    return _LAUNCH_DOC.format(roster=describe_roster(roster))


@dataclass
class _ProjectRoster(WrapperToolset[RunDeps]):
    """The launch tool, described against the roster of the run that is reading it.

    The wrapper exists because the category is assembled once at startup and shared by
    every conversation, while a project's own sub-agents are a fact about the run's
    workspace. `get_tools` is the hook the library calls per request with a `RunContext` in
    hand, which is the earliest point both halves are known.

    Only the *description* moves. The tool's name and schema are the same for every run —
    which is what keeps the operator's catalog (read off `.tools`, a static registry with
    no run to resolve) honest about the agent's real stack, rather than describing a tool
    whose shape depends on where it is called.
    """

    @property
    def id(self) -> str:
        # The library's wrapper answers `None`, which would cost this category its identity
        # for anything keying on it.
        return "subagents"

    @property
    def tools(self) -> dict[str, Any]:
        """The static registry `tools/catalog.py` enumerates for the settings surface.
        Read from the wrapped toolset, because a catalog row is not per-run."""
        return getattr(self.wrapped, "tools", {})

    async def get_tools(self, ctx: RunContext[RunDeps]) -> dict[str, ToolsetTool[RunDeps]]:
        tools = await super().get_tools(ctx)
        tool = tools.get(LAUNCH_TOOL)
        if tool is None:
            # Switched off, or withheld at this level. Nothing to describe.
            return tools
        described = launch_description(await run_roster(ctx))
        if described == tool.tool_def.description:
            # The overwhelmingly common case — no project agent files — and worth taking
            # early: an identical object keeps the request's prefix byte-for-byte stable.
            return tools
        return {
            **tools,
            LAUNCH_TOOL: replace(tool, tool_def=replace(tool.tool_def, description=described)),
        }


def _can_write(spec: SubagentSpec) -> bool:
    """Whether launching this sub-agent is something the operator should rule on.

    Read off the spec's own ceiling rather than from a list of names, so a sub-agent a
    project declares is gated by what it can actually do rather than by whether anyone
    remembered to add it somewhere.
    """
    return spec.permission_ceiling != STRICTEST_PERMISSION


def _parent(ctx: RunContext[RunDeps]) -> SubagentParent:
    """What the launching thread hands the sub-agent.

    Every field narrows it. The permission level rides along because the operator approved
    *this* thread at *that* level, and a sub-agent must not quietly come up with more rope
    than the one that asked for it. The workspace key rides along because that is what
    decides which files it works in — read off the run's own deps rather than re-derived,
    so a sub-agent cannot end up on a different filesystem than the agent that launched it.

    The key rather than a resolved workspace: opening the parent's container or cutting its
    checkout here would be work done to answer a question the launch never asks, and the
    child resolves its own from this key on its first file-tool call, like every run.
    """
    return SubagentParent(
        conversation_id=ctx.deps.conversation_id,
        project_id=ctx.deps.project_id,
        mode=ctx.deps.mode,
        permission=ctx.deps.permission,
        workspace_key=ctx.deps.workspace_key,
        run_id=ctx.deps.run.id,
    )
