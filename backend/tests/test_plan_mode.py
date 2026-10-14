"""Plan mode: the level moving from inside the run, and the three answers to a plan.

The two halves worth pinning are the ones that are easy to get subtly wrong and impossible
to see from the outside. **Entering binds within the turn** — the conversation row is the
durable half, but a model whose catalog only narrowed on the *next* turn could announce
plan mode and keep editing for the rest of this one. And **approving is one act**: the
operator's yes records the plan, seeds the task list and raises the level, and a path that
did any one of those without the others is a thread that can act with nothing agreed, or
has agreed to something it cannot act on.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest
from pydantic_ai import RunContext
from pydantic_ai.exceptions import ApprovalRequired
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from core.container import ServiceContainer
from core.db import init_db, make_engine
from core.vault import Vault
from runs import EventBody, PermissionChanged, PlanUpdated, Run, RunStream, TasksUpdated
from services.conversations import ConversationStore
from services.permissions import PermissionLevel
from services.plan_mode import PlanMode
from services.task_list import ConversationTasks
from tools import RunDeps, build_agent_toolsets, core_categories
from tools.plan import plan_toolset

OWNER = "operator"


type _Wired = tuple[PlanMode, ConversationTasks, ConversationStore, str]


@pytest.fixture
async def wired(tmp_path: Path) -> _Wired:
    """A plan-mode service over a real store, plus the conversation it works on."""
    vault = Vault(tmp_path / "keyfile.json")
    await vault.setup("correct horse battery staple")
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    conversations = ConversationStore(engine, vault, None)
    tasks = ConversationTasks(engine, vault)
    plans = PlanMode(engine, vault, conversations=conversations, tasks=tasks)
    conversation_id = await conversations.create_conversation(OWNER, permission="auto")
    return plans, tasks, conversations, conversation_id


def _ctx(
    plans: PlanMode,
    tasks: ConversationTasks,
    conversation_id: str | None,
    permission: PermissionLevel,
    kind: str = "chat",
) -> tuple[RunContext[RunDeps], Run]:
    run = Run(id="r-plan", kind=kind, owner_id=OWNER, stream=RunStream())
    caps = ServiceContainer()
    caps.add(plans, as_type=PlanMode)
    caps.add(tasks, as_type=ConversationTasks)
    deps = RunDeps(
        run=run,
        owner_id=OWNER,
        caps=caps,
        conversation_id=conversation_id,
        permission=permission,
        # Empty, and that is the point: `disabled_tools` carries the six sources that are
        # settled for the whole turn, and the level is **not** one of them — the gate reads
        # it live off `permission` above. A fixture that baked it in here would withhold
        # the Plan set for the rest of the run however the level moved, which is the bug
        # this file exists to catch.
        disabled_tools=frozenset(),
    )
    return RunContext(deps=deps, model=TestModel(), usage=RunUsage()), run


async def _call(name: str, args: Mapping[str, object], ctx: RunContext[RunDeps]) -> str:
    toolset = build_agent_toolsets({"plan": plan_toolset()})[0]
    tools = await toolset.get_tools(ctx)
    return cast(str, await toolset.call_tool(name, dict(args), ctx, tools[name]))


async def _offered(ctx: RunContext[RunDeps]) -> set[str]:
    """What the model is actually handed right now, through the real gates.

    The whole core catalog rather than just the plan category, because the question these
    tests ask is what the *level* did to the catalog, and the level's effect is only
    visible on the tools it withholds."""
    stack = build_agent_toolsets(core_categories())[0]
    return set(await stack.get_tools(ctx))


def _bodies(run: Run) -> list[EventBody]:
    return [e.body for e in run.stream.replay()]


async def test_entering_narrows_this_turns_catalog_not_just_the_next(wired: _Wired):
    """The load-bearing half. Both gates re-read `ctx.deps` on every model request, so
    moving the level in place is what makes the narrowing bind before the turn ends —
    without it the model announces plan mode and keeps its editing tools until the next
    turn.

    Asserted against the **resolved catalog** rather than against `disabled_tools`,
    because what matters is what the model is actually offered: the level reaches the gate
    live now, and a test that read the stored set would pass on a build where the gate had
    stopped consulting it."""
    plans, tasks, conversations, conversation_id = wired
    ctx, run = _ctx(plans, tasks, conversation_id, "auto")
    assert "files_write_file" in await _offered(ctx)

    _ = await _call("plan_enter", {"reason": "this rewrites the parser"}, ctx)

    assert ctx.deps.permission == "plan"
    assert "files_write_file" not in await _offered(ctx)
    # ...and durably, so the next turn starts where this one left off.
    assert (await conversations.binding(conversation_id)).permission == "plan"
    assert [b.level for b in _bodies(run) if isinstance(b, PermissionChanged)] == ["plan"]


async def test_moving_the_level_leaves_the_other_withholding_reasons_alone(wired: _Wired):
    """The bug this shape exists to prevent, in both directions.

    `disabled_tools` is a **union** of six sources and records no provenance, so a level
    that edited it could add names but never correctly take them back out: subtracting the
    Plan set on approval would lift every other source's hold on the same names. The
    witness is a tool that is withheld for *two* reasons at once — the operator switched it
    off by hand, and Plan withholds it too — which is exactly the overlap a set difference
    cannot see.
    """
    plans, tasks, _, conversation_id = wired
    ctx, _ = _ctx(plans, tasks, conversation_id, "auto")
    # The operator's own choice, which no level may overturn in either direction.
    ctx.deps.disabled_tools = ctx.deps.disabled_tools | {"files_write_file"}
    assert "files_write_file" not in await _offered(ctx)

    _ = await _call("plan_enter", {"reason": "why not"}, ctx)
    assert "files_write_file" not in await _offered(ctx)

    # ...and still gone after a plan is approved, which is where the old code handed it
    # back: the level widened, but the operator never said this tool could return.
    from tools.plan import _retarget

    _retarget(ctx, "auto")
    assert "files_write_file" not in await _offered(ctx)


async def test_submitting_records_the_plan_and_then_parks(wired: _Wired):
    """Recorded *before* the park, not after the approval: the panel renders the document
    the operator is being asked about, so it has to exist before they are asked."""
    plans, tasks, _, conversation_id = wired
    ctx, run = _ctx(plans, tasks, conversation_id, "plan")

    with pytest.raises(ApprovalRequired):
        _ = await _call(
            "plan_submit",
            {"title": "Rewrite the parser", "body": "## Why\nIt is slow.", "steps": ["one", "two"]},
            ctx,
        )

    stored = await plans.current(OWNER, conversation_id)
    assert stored is not None
    assert stored.title == "Rewrite the parser"
    assert stored.status == "pending"
    assert stored.revision == 1
    assert [b.status for b in _bodies(run) if isinstance(b, PlanUpdated)] == ["pending"]


async def test_resubmitting_after_feedback_is_a_new_revision(wired: _Wired):
    """The panel treats a revised plan as a fresh arrival rather than a redraw, and
    `revision` is what it reads to tell them apart."""
    plans, tasks, _, conversation_id = wired
    ctx, _ = _ctx(plans, tasks, conversation_id, "plan")

    for body in ("first attempt", "second attempt"):
        with pytest.raises(ApprovalRequired):
            _ = await _call("plan_submit", {"title": "T", "body": body, "steps": ["s"]}, ctx)

    stored = await plans.current(OWNER, conversation_id)
    assert stored is not None
    assert stored.revision == 2
    assert stored.body == "second attempt"


async def test_a_second_plan_supersedes_the_first_rather_than_accumulating(wired: _Wired):
    """A thread has **one** plan, whatever happened earlier in it.

    Two ways a thread reaches a second plan, and they are the same write: answering
    feedback on a pending one, and proposing fresh work after an earlier plan was already
    carried out. Both replace the row, because a plan is what this thread is working to
    *now* — and the superseded one is not a thing anyone asks the backend for: every
    version is in the transcript as the arguments of the call that submitted it, with the
    operator's answer beside it, which is the version history that actually gets read.

    The counter is the part worth pinning. It never resets, so it stays a monotonic
    "nth submission in this thread" rather than restarting per plan — which is exactly
    what the panel needs, since it claims its one-shot steal per counter value and a
    reset would let a *new* plan land on a claim the previous one had already spent.
    """
    plans, tasks, _conversations, conversation_id = wired
    ctx, _ = _ctx(plans, tasks, conversation_id, "plan")
    first = {"title": "First", "body": "one", "steps": ["a"]}
    with pytest.raises(ApprovalRequired):
        _ = await _call("plan_submit", first, ctx)
    ctx.tool_call_approved = True
    _ = await _call("plan_submit", first, ctx)
    approved = await plans.current(OWNER, conversation_id)
    assert approved is not None
    assert approved.status == "approved"

    # Later in the same thread: new work, a new plan. The approved one is replaced.
    ctx, _ = _ctx(plans, tasks, conversation_id, "plan")
    with pytest.raises(ApprovalRequired):
        _ = await _call("plan_submit", {"title": "Second", "body": "two", "steps": ["b"]}, ctx)

    current = await plans.current(OWNER, conversation_id)
    assert current is not None
    assert current.title == "Second"
    assert current.status == "pending"
    # Counting submissions, not resetting per plan — see the docstring.
    assert current.revision == 2
    # The first plan's task list survives until the second is *approved*: a pending plan
    # has changed nothing yet, and wiping the work in flight on the strength of a proposal
    # the operator has not agreed to would be acting on it early.
    assert [i.content for i in await tasks.items(OWNER, conversation_id)] == ["a"]

    ctx.tool_call_approved = True
    _ = await _call("plan_submit", {"title": "Second", "body": "two", "steps": ["b"]}, ctx)
    assert [i.content for i in await tasks.items(OWNER, conversation_id)] == ["b"]


async def test_only_one_plan_can_be_awaiting_an_answer_at_a_time(wired: _Wired):
    """There is no way to open a second question while the first is unanswered.

    `plan_submit` defers, which parks the turn — so a turn cannot reach a second call,
    and a conversation runs one turn at a time. The unique constraint on the row is the
    same fact spelled in the schema: a thread cannot hold two plans, pending or otherwise.
    """
    from sqlmodel import Session, select

    from models.plan import ConversationPlan

    plans, tasks, _, conversation_id = wired
    ctx, _ = _ctx(plans, tasks, conversation_id, "plan")
    for body in ("first", "second", "third"):
        with pytest.raises(ApprovalRequired):
            _ = await _call("plan_submit", {"title": "T", "body": body, "steps": ["s"]}, ctx)

    with Session(plans.engine) as session:
        rows = session.exec(select(ConversationPlan)).all()
    assert len(rows) == 1
    assert rows[0].revision == 3


async def test_approving_is_one_act(wired: _Wired):
    """Records the yes, seeds the tasks, raises the level — and widens this turn's catalog
    so the same turn carries the plan out. A path that did any one without the others
    leaves a thread that can act with nothing agreed, or has agreed to something it cannot
    act on."""
    plans, tasks, conversations, conversation_id = wired
    ctx, run = _ctx(plans, tasks, conversation_id, "plan")
    args = {"title": "Rewrite", "body": "## Why", "steps": ["read it", "change it"]}
    with pytest.raises(ApprovalRequired):
        _ = await _call("plan_submit", args, ctx)

    # What Pydantic AI does on the re-invocation after the operator approves.
    ctx.tool_call_approved = True
    result = await _call("plan_submit", args, ctx)

    stored = await plans.current(OWNER, conversation_id)
    assert stored is not None and stored.status == "approved"
    assert [i.content for i in await tasks.items(OWNER, conversation_id)] == [
        "read it",
        "change it",
    ]
    assert (await conversations.binding(conversation_id)).permission == "auto"
    # The turn that resumes is the turn that executes, so the tools have to be back now —
    # read through the gate, which is where the level is applied.
    assert ctx.deps.permission == "auto"
    assert "files_write_file" in await _offered(ctx)
    assert "auto" in result

    bodies = _bodies(run)
    assert [b.level for b in bodies if isinstance(b, PermissionChanged)] == ["auto"]
    assert [b.status for b in bodies if isinstance(b, PlanUpdated)] == ["pending", "approved"]
    # The seeded list goes out on the same event a written one does, so the panel cannot
    # tell them apart — which is right: by then it is simply the thread's task list.
    assert [b for b in bodies if isinstance(b, TasksUpdated)]


async def test_settling_moves_the_status_and_nothing_else(wired: _Wired):
    """What a denial and a revision request do. Neither touches the level: only approval
    moves it, and it moves it through `approve`."""
    plans, tasks, conversations, conversation_id = wired
    ctx, _ = _ctx(plans, tasks, conversation_id, "plan")
    with pytest.raises(ApprovalRequired):
        _ = await _call("plan_submit", {"title": "T", "body": "B", "steps": ["s"]}, ctx)
    _ = await conversations.set_permission_level(conversation_id, "plan")

    run = Run(id="r-settle", kind="chat", owner_id=OWNER, stream=RunStream())
    for status in ("revising", "denied"):
        settled = await plans.settle(OWNER, conversation_id, status, run=run)
        assert settled is not None and settled.status == status
        # The document survives its own rejection: a revision is written against it, and
        # a rejected plan is worth still being able to read.
        assert settled.body == "B"
        assert (await conversations.binding(conversation_id)).permission == "plan"


async def test_a_run_with_no_conversation_says_so_rather_than_parking(wired: _Wired):
    """Plan mode belongs to a thread. A stateless run has no operator to park on, so
    saying the capability is not there beats a park nobody can answer."""
    plans, tasks, _, _ = wired
    ctx, _ = _ctx(plans, tasks, None, "auto")

    assert "not available" in await _call("plan_enter", {"reason": "x"}, ctx)
    assert "not available" in await _call(
        "plan_submit", {"title": "T", "body": "B", "steps": ["s"]}, ctx
    )


async def test_an_unattended_run_is_not_offered_plan_mode_at_all(wired: _Wired):
    """Both halves of the guard, because either alone leaves a run that hangs.

    A scheduled task's whole point is that nobody is watching it, so a `plan_submit`
    park there waits until the process restarts — and `plan_enter` is worse than a park:
    it takes every mutating tool away and leaves that park as the only way back, so the
    run could neither act nor ever be released.
    """
    from services.tool_policy import lane_disabled_tools

    # The catalog gate, which is what actually prevents it.
    withheld = lane_disabled_tools("task")
    assert {"plan_enter", "plan_submit"} <= withheld
    # Reading a plan reaches nothing and strands nobody, so it stays.
    assert "plan_read" not in withheld

    # And the belt-and-braces inside the call, for the same reason `builtin.py` carries
    # one: "unreachable" and "hangs until restart" are too far apart to leave to one gate.
    plans, tasks, _, conversation_id = wired
    ctx, _ = _ctx(plans, tasks, conversation_id, "auto", kind="task")
    assert "unattended" in await _call("plan_enter", {"reason": "x"}, ctx)
    assert "unattended" in await _call(
        "plan_submit", {"title": "T", "body": "B", "steps": ["s"]}, ctx
    )
    # Nothing moved: no plan recorded, and the level is where it was.
    assert await plans.current(OWNER, conversation_id) is None
    assert ctx.deps.permission == "auto"


async def test_a_plan_that_could_not_be_stored_is_not_put_to_the_operator(wired: _Wired):
    """No document, no question.

    A locked vault drops the write *and* the announcement, so parking anyway would face
    the operator with an approval whose panel says there is no plan — the store is also
    what a reload reads — leaving Stop as the only way out. It says so to the model
    instead, and the turn carries on able to act.
    """
    plans, tasks, conversations, conversation_id = wired
    ctx, run = _ctx(plans, tasks, conversation_id, "plan")
    plans.vault.lock()

    before = (await conversations.binding(conversation_id)).permission
    result = await _call("plan_submit", {"title": "T", "body": "B", "steps": ["s"]}, ctx)

    assert "could not be stored" in result
    # No document, and nothing announced either — the panel is not told to draw a plan
    # that was never written.
    assert not [b for b in _bodies(run) if isinstance(b, PlanUpdated)]
    assert await plans.current(OWNER, conversation_id) is None
    # And nothing moved: a refusal is not an approval, so the level stays put.
    assert (await conversations.binding(conversation_id)).permission == before
    assert not [b for b in _bodies(run) if isinstance(b, PermissionChanged)]


async def test_a_locked_vault_does_not_block_the_yes(wired: _Wired):
    """The operator read the plan and said so; the transcript holds what they agreed to
    either way. Refusing here would strand a thread on a decision that has been made."""
    plans, tasks, conversations, conversation_id = wired
    ctx, _ = _ctx(plans, tasks, conversation_id, "plan")
    with pytest.raises(ApprovalRequired):
        _ = await _call("plan_submit", {"title": "T", "body": "B", "steps": ["s"]}, ctx)
    plans.vault.lock()

    ctx.tool_call_approved = True
    _ = await _call("plan_submit", {"title": "T", "body": "B", "steps": ["s"]}, ctx)

    assert (await conversations.binding(conversation_id)).permission == "auto"


async def test_the_plan_is_sealed_at_rest(wired: _Wired):
    from sqlmodel import Session, select

    from models.plan import ConversationPlan

    plans, tasks, _, conversation_id = wired
    ctx, _ = _ctx(plans, tasks, conversation_id, "plan")
    with pytest.raises(ApprovalRequired):
        _ = await _call(
            "plan_submit",
            {"title": "T", "body": "rewrite acme's billing", "steps": ["s"]},
            ctx,
        )

    with Session(plans.engine) as session:
        row = session.exec(select(ConversationPlan)).one()
    # The plan names the operator's files and restates what they asked for.
    assert "acme" not in row.plan_enc
    # ...but the status is not content, and a locked vault must still be able to report
    # that a thread is waiting on an answer.
    assert row.status == "pending"


async def test_the_two_backfills_are_reachable_and_shaped_as_the_panel_reads_them(
    monkeypatch: pytest.MonkeyPatch,
):
    """The REST half of both surfaces, on a booted app.

    A client opening or reloading a thread has no stream to replay, so these are what the
    panels start from — and a plan awaiting approval is the last thing that should vanish
    because the operator refreshed the page. Asserted together because they are one
    change: the task list moved to `/tasks` so the plan could have `/plan`, and a build
    where only one of them moved answers 404 on the other.
    """
    from ._helpers import app_state, client_app, collect_sse_events, patch_model_resolution
    from ._json import json_body, json_list, obj, parse_json, s

    patch_model_resolution(monkeypatch, output_text="hello")
    async with client_app() as (client, app):
        created = await client.post("/chat", json={"prompt": "hi"})
        conversation_id = s(json_body(created)["conversation_id"])
        _ = await collect_sse_events(client, s(json_body(created)["run_id"]))

        # Empty rather than missing: a thread with neither is the ordinary case.
        assert json_list(await client.get(f"/conversations/{conversation_id}/tasks")) == []
        plan_response = await client.get(f"/conversations/{conversation_id}/plan")
        assert parse_json(plan_response.content) is None

        _ = await app_state(app, "plan_mode", PlanMode).submit(
            "operator",
            conversation_id,
            title="Rewrite the parser",
            body="## Why\nIt is slow.",
            steps=["read it", "change it"],
        )
        _ = await app_state(app, "conversation_tasks", ConversationTasks).seed(
            "operator", conversation_id, ["read it", "change it"]
        )

        plan = json_body(await client.get(f"/conversations/{conversation_id}/plan"))
        assert plan["title"] == "Rewrite the parser"
        assert plan["steps"] == ["read it", "change it"]
        assert plan["status"] == "pending"
        assert plan["revision"] == 1

        tasks = [
            obj(t) for t in json_list(await client.get(f"/conversations/{conversation_id}/tasks"))
        ]
        assert [t["content"] for t in tasks] == ["read it", "change it"]
        assert all(t["status"] == "pending" for t in tasks)


def test_the_two_refusals_say_different_things():
    """A denial that read as "try again" produces a second attempt at something already
    refused; a revision request that read as a flat no produces an apology and a stop."""
    from routes.runs import refusal_message

    revise = refusal_message("revise", "use the existing helper")
    assert "use the existing helper" in revise
    assert "propose again" in revise
    assert "do not abandon" in revise.lower()

    deny = refusal_message("deny", "not now")
    assert "denied" in deny
    assert "propose again" not in deny
    # Both work with nothing written: the operator need not explain themselves.
    assert refusal_message("revise", None)
    assert refusal_message("deny", None)
