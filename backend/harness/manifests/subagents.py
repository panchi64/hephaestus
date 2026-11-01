"""Spawned agents: the runtime behind the `agents` category's delegation tools.

A spawned agent is a hidden conversation composed by the same ``compose_turn`` as an
operator's message, so this manifest adds no engine:

- the **launcher** (``_subagents.py``), which tools resolve by its abstract type because
  turn composition sits above ``tools/``;
- the **stopper** (``_subagent_stop.py``), the operator's stop cascade and the marks a launch
  in flight reads;
- the **wake** (``_subagent_wake.py``), which carries reports and messages between the
  agents of a tree;
- the **register** (``services/subagent_store.py``), which keeps the tree's links once the
  runs are gone. Its startup pass closes out rows a previous process left live.
"""

from __future__ import annotations

from harness.manifest import (
    FeatureManifest,
    FeatureRuntime,
    HarnessContext,
)
from harness.manifests._inbox_dispatch import InboxDispatcher
from harness.manifests._subagent_stop import SubagentStopper
from harness.manifests._subagent_wake import SubagentWake
from harness.manifests._subagents import ConversationSubagents
from routes.subagents import router as subagents_router
from runs import RunRegistry
from services.subagent_store import SubagentStore
from services.subagents import SubagentLauncher, SubtreeStopper
from tools.report import report_toolset


async def _build(ctx: HarnessContext) -> FeatureRuntime:
    records = SubagentStore(ctx.engine, ctx.vault)
    wake = SubagentWake(ctx, records, ctx.services.get(InboxDispatcher))
    stopper = SubagentStopper(records, wake, ctx.services.get(RunRegistry))
    launcher = ConversationSubagents(ctx, records, wake, stopper)
    # Waits for the key, like every startup pass touching sealed rows.

    async def reconcile() -> None:
        _ = await records.reconcile_stranded()

    _ = ctx.lifecycle.track("subagent-reconcile", reconcile())
    return FeatureRuntime(
        services=(records, launcher),
        # On the app too: the panel's routes read the register from `app.state`, not the
        # agent's bag.
        state={"subagent_records": records, "subagent_wake": wake},
        # Under the abstract types so `tools/subagents.py` resolves it without importing this
        # wiring layer, which sits above `tools/`.
        capabilities=((launcher, SubagentLauncher), (stopper, SubtreeStopper)),
        # Carries a finished report back to the launching thread.
        run_terminal=(wake.settled,),
    )


MANIFEST = FeatureManifest(
    name="subagents",
    composes_turns=True,
    # A thread property under `/conversations`, reached with the grant that reads the
    # thread, so no scope of its own.
    routers=(subagents_router,),
    # Its own category so it stays out of the dormant `agents` group: a spawned agent always
    # has the tool it finishes with.
    toolsets=(("subagent", report_toolset),),
    # Launching composes a full interactive turn, so contributing features build first (as for
    # the scheduler's executor).
    after=(
        "agents",
        "inbox",
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
    build=_build,
)
