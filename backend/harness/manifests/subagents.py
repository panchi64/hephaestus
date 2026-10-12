"""The sub-agents feature — autonomous agents the model launches for itself.

What a sub-agent *is* is almost nothing: a hidden conversation, composed by the same
``compose_turn`` an operator's own message goes through, running the same engine over the
same tools. So this manifest contributes three small things and no machinery.

The **launcher** (built in ``_subagents.py`` beside this file), so ``tools/subagents.py`` can start
one from inside a run without importing turn composition, which sits above ``tools/``. It is built
here for the same reason the scheduler's task executor is: a tool has no ``Request``, and a launcher
that closed over one could never be reached from inside a run, while here every handle it needs is
already resolved.

The **register** (``services/subagent_store.py``), which is what keeps a sub-agent's
transcript reachable by name once the process that ran it is gone. Its startup pass closes
out anything a previous process left running — a row still live after a restart describes
work that stopped when the process did, and left alone it would count against the operator's
cap and promise a report that is never coming.

**A sub-agent's thread notifies like any other**, because it *is* one, and its approvals
arrive through the ordinary approval route: a parked run is addressed by run id, so a
sub-agent waiting on the operator needs no surface of its own.
"""

from __future__ import annotations

from harness.manifest import (
    DormantCategory,
    FeatureManifest,
    FeatureRuntime,
    HarnessContext,
)
from harness.manifests._subagent_wake import SubagentWake
from harness.manifests._subagents import ConversationSubagents
from routes.subagents import router as subagents_router
from services.subagent_store import SubagentStore
from services.subagents import SubagentLauncher
from tools.subagents import GATED_TOOLS, subagents_toolset


async def _build(ctx: HarnessContext) -> FeatureRuntime:
    records = SubagentStore(ctx.engine, ctx.vault)
    launcher = ConversationSubagents(ctx, records)
    wake = SubagentWake(ctx, records)
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
    # task executor works under, and for the same reason. Research is deliberately not on
    # it: it contributes nothing to a turn any more, having handed its launcher to this
    # feature and kept only a one-shot migration and a scope name.
    after=(
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
    # No scope of its own, and deliberately not a new one: a sub-agent is a conversation,
    # created and streamed and read through `/chat` and `/conversations` like every other,
    # so it reaches nothing the chat scope does not already cover. A scope claiming no
    # prefix would grant nothing while adding a name every issued token has to carry.
    toolsets=(("subagents", subagents_toolset),),
    # Launching a sub-agent that can change things is the operator's to allow. The tool
    # raises for approval either way, but a name missing from the assembled gated set never
    # reaches the approval scopes — so without this the operator would be asked again on
    # every single launch, with no way to say yes for the conversation.
    gated_tools=GATED_TOOLS,
    dormant=(
        DormantCategory(
            "subagents",
            "hand a self-contained piece of work to another agent that does it on its "
            "own while you carry on, and tells you what it found",
        ),
    ),
    build=_build,
)
