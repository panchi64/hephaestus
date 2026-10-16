"""The agent's own task list — the checklist the operator watches it work through.

``pydantic_ai_harness.Planning`` owns the tools and their machinery; we supply the storage
(``services/task_list`` — sealed, per-conversation), the surface, and the names.

**This is not the plan.** A *plan* here is the written document a Plan-level turn produces
for the operator to approve (``tools/plan.py``); this is the running checklist the agent
keeps for itself while it works, at every level. They were one thing under one word, which
is why the word is spent carefully now: nothing in this module says "plan" to the model.

**Registered as a toolset, not as a capability, on purpose.** The capability form also
injects the list as a tail reminder through its own model-request hook. Taking it whole
would have put the tools outside the one thing every other tool passes through: the
namespaced, operator-toggleable catalog (``tools/catalog.py``) whose promise is that the
settings list and the agent's actual stack cannot diverge. So the toolset comes from the
capability and the reminder is re-delivered through the seam this codebase already has for
exactly this — a ``PromptContextProvider``, which lands at the *tail* of the turn's prompt
for the same prompt-cache reason the harness places it there.

**Three tools, not six.** ``write`` already replaces the whole list, so ``add_task``/
``remove_task`` are the same edit spelled longer, and ``update_statuses`` covers
``update_task_status`` with a list of one. Each dropped tool cost a name, a description and
a JSON schema on every request to buy the model a second way to do something it could
already do — and a second way is a decision it has to make.

**The three are renamed on the way out** (:data:`UPSTREAM`). The harness registers them by
hardcoded string — ``write_plan``, ``update_task_statuses``, ``read_plan`` — and validates
any allowlist against those, so the names cannot be chosen upstream. Prefixed by the
category they would reach the model as ``tasks_write_plan``: the exact collision this split
exists to remove, restated in the one place the model actually reads. So the toolset maps
the names in ``get_tools`` and maps them back in ``call_tool``, and the harness never sees
a name it did not register.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import replace
from typing import cast, override

from pydantic_ai import AbstractToolset, ModelRetry, RunContext, Tool, ToolsetTool
from pydantic_ai_harness.planning import InMemoryPlanStore, Planning, TaskStatus
from pydantic_ai_harness.planning import PlanStore as TaskStoreProtocol

from core.diagnostics import render_tool_error
from services.task_list import ConversationTasks, ConversationTaskStore, render_tasks

from .deps import PromptContextRequest, RunDeps
from .describe import registered_tools
from .harness_events import attributable

# Rendered above the list at the tail of the turn. Short and fixed: the block's *content*
# changes constantly, so anything static about it belongs here rather than in the churn.
_PREAMBLE = (
    "Your task list for this conversation, as the operator sees it. Keep it current: mark "
    "a task `in_progress` when you start it and `completed` when it is done."
)


# One bound toolset per conversation, kept because building one registers the tools and
# generates their schemas. Bounded so a long-lived process doesn't retain an entry for
# every thread ever opened.
_MAX_BOUND = 64

#: Our name for each harness tool. The keys are what the model is offered (prefixed by the
#: category, so ``tasks_write``); the values are what the harness registered and the only
#: names it will answer to. The harness validates both the allowlist and the description
#: keys against the tools it registers, so a rename upstream fails loudly here instead of
#: silently offering more than we intended.
UPSTREAM = {
    "write": "write_plan",
    "update_statuses": "update_task_statuses",
    "read": "read_plan",
}

#: The inverse, for mapping a call back to the name the harness knows.
LOCAL = {upstream: local for local, upstream in UPSTREAM.items()}

DESCRIPTIONS = {
    "write_plan": (
        "Creates or replaces your task list for this conversation: the checklist the "
        "operator watches beside the chat while you work. Use it for any work of three or "
        "more steps, and call it before you start the first one so the operator sees the "
        "shape of the work up front. Skip it for a single action or a quick answer, where "
        "a list is noise.\n\n"
        "Pass the whole list on every call, unchanged and finished tasks included, because "
        "the call replaces what was there. Keep exactly one task `in_progress`: the one "
        "you are doing now. Give each task a short `id` of your own (`migrate`, "
        "`backfill`) so `tasks_update_statuses` can name it later; an omitted id is "
        "generated, and only `tasks_read` shows it.\n\n"
        "Call it again when the work changes shape: a step you discover goes in, and one "
        "that turns out unnecessary is marked `cancelled` rather than dropped, so the "
        "operator sees it was considered. For ordinary progress, use "
        "`tasks_update_statuses` instead of rewriting the list.\n\n"
        "This is not the plan from plan mode. That is a document the operator approves; "
        "on approval its steps become this list.\n\n"
        "Returns the list as stored, with a progress line."
    ),
    "update_task_statuses": (
        "Changes the status of one or more tasks by `id`, without rewriting the list. "
        "Call it as you move through the work: in one call, mark the task you just "
        "finished `completed` and the next one `in_progress`, so the list the operator "
        "watches never lags behind you.\n\n"
        "Mark a task `completed` only when it is done, not while its tests fail or part of "
        "it is missing. Mark one you decided against `cancelled`, and say why in your "
        "reply. Before you give your final answer, every task should be `completed` or "
        "`cancelled`; an answer that leaves one open is sent back once so you can finish "
        "it or explain it.\n\n"
        "Entries apply in order, so list a completion before the start that follows it. "
        "The batch is checked as a unit: if any `id` is unknown, nothing changes and the "
        "errors come back."
    ),
    "read_plan": (
        "Returns your task list with each task's `id`, status and content, and a progress "
        "line. The list is already at the end of your context each turn, without ids, so "
        "call this only when you need the id of a task you did not name yourself."
    ),
}


def _planning_toolset(store: TaskStoreProtocol) -> AbstractToolset[RunDeps]:
    toolset = _planning(store).get_toolset()
    assert isinstance(toolset, AbstractToolset)
    return toolset


def _planning(store: TaskStoreProtocol) -> Planning[RunDeps]:
    """A ``Planning`` over ``store``, narrowed to the three tools we offer."""
    return Planning[RunDeps](
        store_resolver=lambda _ctx: store,
        tools=tuple(UPSTREAM.values()),
        descriptions=DESCRIPTIONS,
    )


def _store_for(ctx: RunContext[RunDeps]) -> TaskStoreProtocol:
    """The store this run's tasks live in — sealed and per-conversation where there is
    one, in memory for the life of the run where there isn't."""
    tasks = ctx.deps.caps.get_optional(ConversationTasks)
    conversation_id = ctx.deps.conversation_id
    if tasks is None or conversation_id is None:
        # No conversation to key on (a one-off run), or no store wired: keep the list in
        # memory. The tools still work; the list simply doesn't outlive the run.
        return InMemoryPlanStore()
    return ConversationTaskStore(
        tasks,
        owner_id=ctx.deps.run.owner_id,
        conversation_id=conversation_id,
        run=ctx.deps.run,
    )


class ConversationTaskToolset(AbstractToolset[RunDeps]):
    """The ``tasks`` category, rebound to whichever conversation is asking.

    **The rebinding is the whole point.** ``Planning.resolve_store`` memoises its store on
    the capability instance the first time it is asked, and a category object is built once
    for the whole app — so a single shared capability would hand *every* conversation the
    first one's tasks: thread B would read and overwrite thread A's list. Registering the
    capability's toolset directly is only safe under ``for_run()``, the per-run clone hook
    that a toolset registration never reaches. So each conversation gets its own capability
    (hence its own memoised store), resolved here.

    Shaped like ``tools/files.py``: ``get_tools`` answers from a template, because a tool's
    definition doesn't depend on whose list it will touch — which keeps the offered set,
    the operator catalog and the enabled gate identical for every thread.

    It is also where the harness's names become ours (see the module note): every path out
    of here speaks :data:`UPSTREAM`'s keys, and every path back in speaks its values.
    """

    def __init__(self, template: AbstractToolset[RunDeps]) -> None:
        self._template: AbstractToolset[RunDeps] = template
        self._bound: OrderedDict[str, tuple[AbstractToolset[RunDeps], TaskStoreProtocol]] = (
            OrderedDict()
        )

    @property
    @override
    def id(self) -> str:
        return "tasks"

    @property
    def tools(self) -> dict[str, Tool[RunDeps]]:
        """The static registry ``tools/catalog.py`` enumerates for the settings surface.

        Re-keyed like everything else here: the operator's tool list and the names the
        model is offered have to be the same words, or a tool switched off in settings
        would not be the tool withheld from the agent.
        """
        registry = registered_tools(self._template)
        return {LOCAL[name]: tool for name, tool in registry.items() if name in LOCAL}

    @override
    async def get_tools(self, ctx: RunContext[RunDeps]) -> dict[str, ToolsetTool[RunDeps]]:
        tools = await self._template.get_tools(ctx)
        return {
            local: replace(tool, tool_def=replace(tool.tool_def, name=local))
            for upstream, tool in tools.items()
            if (local := LOCAL.get(upstream)) is not None
        }

    @override
    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, object],
        ctx: RunContext[RunDeps],
        tool: ToolsetTool[RunDeps],
    ) -> object:
        bound, store = self._for(ctx)
        ctx.deps.tasks_touched = True
        # The store is cached with the toolset, but the run it emits on is per turn — point
        # it at the live one, or the second turn's updates stream onto a dead run.
        if isinstance(store, ConversationTaskStore):
            store.bind_run(ctx.deps.run)
        # Back to the harness's own name, and re-resolve against the bound toolset: the
        # tool handed in carries the template's function, which is wired to the template's
        # (unbound) store.
        upstream = UPSTREAM[name]
        # Named as the catalog names it, so the planning tools' own capability events can be
        # attributed — a prefixed toolset otherwise hands the harness an unprefixed
        # `tool_name` the library cannot look up (`tools/harness_events.py`).
        inward = attributable(ctx, f"{self.id}_{name}")
        return cast(
            object,
            await bound.call_tool(
                upstream, tool_args, inward, (await bound.get_tools(ctx))[upstream]
            ),
        )

    def _for(self, ctx: RunContext[RunDeps]) -> tuple[AbstractToolset[RunDeps], TaskStoreProtocol]:
        # Keyed by conversation where there is one, else by run: a conversation's tasks
        # outlive its turns, a one-off run's do not.
        key = ctx.deps.conversation_id or f"run:{ctx.deps.run.id}"
        entry = self._bound.pop(key, None)
        if entry is None:
            store = _store_for(ctx)
            entry = (_planning_toolset(store), store)
            if len(self._bound) >= _MAX_BOUND:
                _ = self._bound.popitem(last=False)
        self._bound[key] = entry
        return entry


def tasks_toolset() -> AbstractToolset[RunDeps]:
    """The ``tasks`` category — the model's read/write access to its own task list."""
    # The template's store is never read or written: only `call_tool` acts, and it always
    # rebinds first. It exists to carry the tool definitions.
    return ConversationTaskToolset(_planning_toolset(InMemoryPlanStore()))


_OPEN = frozenset({TaskStatus.pending, TaskStatus.in_progress})


async def remind_of_open_tasks[OutputT](ctx: RunContext[RunDeps], output: OutputT) -> OutputT:
    """Send a final answer back, once a turn, while the task list still has open steps.

    A model that writes a list and then answers before working through it has declared
    victory early, and the list is the one record of what it said it would do. Once per
    turn, so a step that cannot be done ends up named in the answer rather than looping,
    and only in a run that worked on the list: one left open by earlier work is not what
    a later, unrelated question was about.
    """
    deps = ctx.deps
    if not isinstance(output, str) or deps.open_tasks_reminded or not deps.tasks_touched:
        return output
    tasks = ctx.deps.caps.get_optional(ConversationTasks)
    conversation_id = ctx.deps.conversation_id
    if tasks is None or conversation_id is None:
        return output
    items = await tasks.items(ctx.deps.run.owner_id, conversation_id)
    still_open = [item for item in items if item.status in _OPEN]
    if not still_open:
        return output
    ctx.deps.open_tasks_reminded = True
    steps = "; ".join(f"{item.content} ({item.status.value})" for item in still_open)
    raise ModelRetry(
        render_tool_error(
            f"your task list still has open steps: {steps}",
            hints=[
                "finish them, then answer",
                "or mark each one `completed` or `cancelled` with `tasks_update_statuses` "
                + "and say in your answer why it is not done",
            ],
        )
    )


async def tasks_context(req: PromptContextRequest) -> str:
    """The current task list, for the tail of this turn's prompt.

    Delivered per turn rather than written into history: the list changes on nearly every
    step, and a changing block at the head of the request would invalidate the inference
    engine's prompt-prefix cache for the whole conversation behind it.
    """
    tasks = req.caps.get_optional(ConversationTasks)
    if tasks is None or req.conversation_id is None:
        return ""
    items = await tasks.items(req.owner_id, req.conversation_id)
    # No tasks, no block. This is also what keeps an operator who disabled the `tasks`
    # category from being told to "keep it accurate" with no tools registered to do so:
    # with the tools gone nothing can create a task, so there is nothing to render. A list
    # left over from before they disabled it still shows — it describes outstanding work,
    # and hiding it would be the more surprising of the two.
    if not items:
        return ""
    return f"{_PREAMBLE}\n\n{render_tasks(items)}"
