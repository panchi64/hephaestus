"""Routines: the scheduler, how a routine fires, and the agent's `routines_*` tools.

An agent routine runs as an ordinary Run in a new thread each time, by the routine's agent,
through the chat turn's own composition (no forked submission path); a reminder fires its
prompt verbatim as a notification. The executor lives in :mod:`.executor`.
"""

from __future__ import annotations

import logging

from agent.recompose import ParkedFacts, Recomposed, RecomposeError, register_recompose
from core.api_scopes import ScopeClaim
from harness.manifest import (
    DormantCategory,
    FeatureManifest,
    FeatureRuntime,
    HarnessContext,
    PluginInfo,
)
from harness.turns import UnattendedTurns
from routes.deps import OPERATOR_ID
from runs import Run, RunKind, RunRegistry
from services.agents import AgentCatalog
from services.approval_grants import ApprovalGrantStore
from services.conversations import ConversationStore
from services.corpus import CorpusIndex
from services.notifications import NotificationService
from services.plugins.contributions.background import Background
from services.registry import ModelRegistry
from services.tool_sensitivity import Sensitivity
from tools.platform import PlatformSource

from . import routes as tasks_routes
from .executor import TASK_KIND, RoutineFires, RunWaiters
from .scheduler import SchedulerService
from .services import RoutineStore, ScheduleNaturalLanguage
from .services.corpus import SOURCE_KIND, SURFACE_ID, routine_runs_adapter
from .tools import GATED_TOOLS as ROUTINES_GATED
from .tools import routines_toolset

logger = logging.getLogger(__name__)

_SENSITIVITY = {
    "routines_list": Sensitivity.READ,
    # A routine an agent drafts never fires until the operator approves it; rewriting or
    # deleting an approved one is gated on the tool itself.
    "routines_delete": Sensitivity.WORKSPACE_WRITE,
    "routines_write": Sensitivity.WORKSPACE_WRITE,
    # Starts a routine's agent turn, with its pre-authorizations, after this one.
    "routines_run_now": Sensitivity.HOST_EXEC,
}


async def _build(ctx: HarnessContext) -> FeatureRuntime:
    services = ctx.services
    registry = services.get(ModelRegistry)
    runs = services.get(RunRegistry)
    notifications = services.get(NotificationService)
    routines = RoutineStore(ctx.engine, ctx.vault, services.get(AgentCatalog))
    services.get(CorpusIndex).register(routine_runs_adapter(ctx.engine, ctx.vault))
    turns = UnattendedTurns(ctx)
    waiters = RunWaiters()
    fires = RoutineFires(
        conversations=services.get(ConversationStore),
        grants=services.get(ApprovalGrantStore),
        notifications=notifications,
        routines=routines,
        turns=turns,
        waiters=waiters,
    )

    async def _recompose(facts: ParkedFacts) -> Recomposed:
        if (gone := await fires.routine_gone(facts.run.id)) is not None:
            raise RecomposeError(gone)
        return await turns.composer.recompose(facts, kind=TASK_KIND, depth=0)

    register_recompose(runs, TASK_KIND, _recompose)

    # Single-instance, in-process. Prompts are encrypted, so its tick loop parks while the
    # vault is locked. This manifest builds after everything the executor reaches, so the
    # scheduler stops before all of it and nothing submits runs into a tearing-down process.
    scheduler = SchedulerService(
        ctx.engine,
        ctx.vault,
        executor=fires.execute,
        notify=fires.notify,
    )
    fires.attach(scheduler)

    async def _stop_scheduler() -> None:
        # Parked fires are released first: their waiting executors would hold the stop.
        waiters.release_parked(runs)
        await scheduler.stop()

    ctx.lifecycle.on_stop("task-settling", fires.drain_settling)

    async def _restore_problem(run: Run) -> str | None:
        return await fires.routine_gone(run.id)

    runs.set_restore_check(TASK_KIND, _restore_problem)

    async def _cancel_runs_of(routine_id: str, by_run: str | None) -> None:
        for run_id in await scheduler.open_run_ids(routine_id):
            if run_id != by_run:
                _ = await runs.cancel(run_id)

    routines.before_delete(_cancel_runs_of)

    async def _close_stranded() -> None:
        _ = await ctx.vault.unlocked_event.wait()
        _ = await runs.parks_restored.wait()
        try:
            closed = await scheduler.close_stranded(lambda run_id: runs.get(run_id) is not None)
        except Exception:
            logger.exception("tasks: could not close the stranded TaskRun rows")
            return
        if closed:
            logger.info("tasks: closed %d TaskRun row(s) the last process left open", closed)

    _ = ctx.lifecycle.track("task-stranded-runs", _close_stranded())

    async def _resolve_model():
        resolved = await registry.resolve_background(owner_id=OPERATOR_ID)
        return resolved.model, resolved.reasoning_off

    parser = ScheduleNaturalLanguage(resolve_model=_resolve_model)
    return FeatureRuntime(
        services=(scheduler, routines, parser, waiters),
        capabilities=(scheduler, routines),
        contributions=(Background("scheduler", scheduler.start, _stop_scheduler),),
        run_terminal_sync=(fires.on_run_terminal,),
    )


MANIFEST = FeatureManifest(
    name="routines",
    plugin=PluginInfo(
        title="Routines",
        description=(
            "Runs prompts on a schedule (once, interval, cron or webhook), each in a new "
            + "chat, and fires reminders."
        ),
    ),
    composes_turns=True,
    # The executor composes the full interactive capability set, so those features build first.
    after=(
        "agents",
        "calendar",
        "corpus",
        "external",
        "memory",
        "notifications",
        "secret-vault",
        "skills",
        "uploads",
        "views",
        "web",
    ),
    routers=(tasks_routes.router,),
    toolsets=(("routines", routines_toolset),),
    dormant=(
        DormantCategory(
            "routines",
            "schedule work to run later or on repeat — drafting routines, listing them, "
            + "running one now",
        ),
    ),
    gated_tools=ROUTINES_GATED,
    api_scopes=(ScopeClaim("tasks", ("/tasks",)),),
    # The webhook trigger is auth-exempt: the per-task unguessable path token is the
    # credential. Every other `/tasks` route stays behind the gate.
    public_prefixes=("/tasks/hooks",),
    tool_sensitivity=_SENSITIVITY,
    # Queues like interactive work in slots of its own, with nobody in front of it.
    run_kinds={TASK_KIND: RunKind("routine", chat_turn=True)},
    # A reminder is worthless if it only lands somewhere the operator isn't looking.
    out_of_band_kinds=frozenset({"reminder"}),
    platform_sources={"routines": PlatformSource((SOURCE_KIND,), SURFACE_ID)},
    build=_build,
)
