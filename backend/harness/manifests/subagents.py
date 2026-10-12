"""Spawned agents — the runtime behind the `agents` category's delegation tools.

A spawned agent is a hidden conversation composed by the same ``compose_turn`` an
operator's message goes through, so this manifest contributes no engine:

- the **launcher** (``_subagents.py``), which tools resolve by its abstract type because
  turn composition sits above ``tools/``;
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
from harness.manifests._subagent_wake import SubagentWake
from harness.manifests._subagents import ConversationSubagents
from routes.subagents import router as subagents_router
from services.subagent_store import SubagentStore
from services.subagents import SubagentLauncher


async def _build(ctx: HarnessContext) -> FeatureRuntime:
    records = SubagentStore(ctx.engine, ctx.vault)
    wake = SubagentWake(ctx, records)
    launcher = ConversationSubagents(ctx, records, wake)
    # Waits for the key like every other startup pass that touches sealed rows, and does
    # nothing on an installation that has never run a sub-agent.
    ctx.lifecycle.track("subagent-reconcile", records.reconcile_stranded())
    return FeatureRuntime(
        services=(records, launcher),
        # On the app as well, because the register is what the panel's own routes read —
        # a card list and a transcript are not agent capabilities, and a route resolves
        # its handles from `app.state` rather than from the agent's bag.
        state={"subagent_records": records, "subagent_wake": wake},
        # Registered under the abstract type so `tools/subagents.py` can resolve it
        # without importing this wiring layer, which sits above `tools/`.
        capabilities=((launcher, SubagentLauncher),),
        # The sync half notes the operator driving the thread themselves, which clears the
        # unattended-turn count; it runs inline so the count is already right by the time
        # anything reacts. The async half carries a finished sub-agent's report back to the
        # thread that launched it, and rescues one the substrate is about to drop.
        run_terminal_sync=(wake.observed,),
        run_terminal=(wake.settled,),
    )


MANIFEST = FeatureManifest(
    name="subagents",
    # One read, under `/conversations` — a thread's sub-agents are a property of the
    # thread, reached with the same grant that reads it, which is why this claims no scope
    # of its own (see the note on `api_scopes` below).
    routers=(subagents_router,),
    # Launching one composes a full interactive turn, so every feature contributing to
    # that turn's capability set must have built first — the same list the scheduler's
    # task executor works under, and for the same reason.
    after=(
        "agents",
        "calendar",
        "corpus",
        "external",
        "mail",
        "memory",
        "notifications",
        "secret-vault",
        "skills",
        "uploads",
        "views",
        "web",
    ),
    # No scope of its own: a spawned agent is a conversation, reached through `/chat` and
    # `/conversations` like every other. Its tools are the `agents` category's.
    build=_build,
)
