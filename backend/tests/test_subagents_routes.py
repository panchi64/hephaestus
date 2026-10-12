"""What the panel reads: a thread's sub-agents, live and finished.

One endpoint, and what these hold is mostly about what it deliberately *isn't*. There is no
sub-agent transcript route, no sub-agent cancel route and no sub-agent approval route,
because a sub-agent is a conversation and a run and those routes already exist — so the
properties worth pinning are the ones that make the card's other links work: every card
carries the ids that reach them, and the list is what survives a restart.
"""

from __future__ import annotations

import asyncio

from services.agents import builtin_roster
from services.subagents import SubagentLauncher
from tests._helpers import client_app, patch_model_resolution

EXPLORER = builtin_roster()["explorer"]


async def _settle(app, run_id: str) -> None:
    run = app.state.runs.get(run_id)
    assert run is not None
    if run.task is not None:
        await run.task
    for _ in range(5):
        pending = list(app.state.run_terminal_tasks)
        if not pending:
            await asyncio.sleep(0)
            if not app.state.run_terminal_tasks:
                return
            continue
        await asyncio.gather(*pending, return_exceptions=True)


async def _launch(app, conversation_id: str, task: str, handle: str = "the-explorer"):
    launcher = app.state.capabilities.get_optional(SubagentLauncher)
    assert launcher is not None
    from services.subagents import SubagentParent

    started = await launcher.launch(
        "operator",
        EXPLORER,
        task,
        handle=handle,
        parent=SubagentParent(conversation_id=conversation_id),
    )
    await _settle(app, started.run_id)
    return started


async def test_a_thread_with_no_subagents_answers_with_an_empty_list(monkeypatch):
    async with client_app() as (client, _app):
        patch_model_resolution(monkeypatch)
        got = await client.get("/conversations/never-delegated/subagents")

        # Not a 404. The panel asks this for every thread the operator opens, and an
        # error for the ordinary case is an error the client has to learn to ignore.
        assert got.status_code == 200
        assert got.json() == {"subagents": []}


async def test_each_card_carries_the_ids_its_other_links_need(monkeypatch):
    async with client_app() as (client, app):
        patch_model_resolution(monkeypatch, output_text="found it in parser.py")
        started = await _launch(app, "parent-1", "find the parser", "parser-finder")

        card = (await client.get("/conversations/parent-1/subagents")).json()["subagents"][0]

        # These three are the whole integration: the transcript is that conversation's
        # messages, and the live tail, the approval and the cancel are all that run's
        # routes. A card missing them would need surfaces of its own for each.
        assert card["id"] == started.subagent_id
        assert card["conversation_id"] == started.conversation_id
        assert card["run_id"] == started.run_id
        assert card["name"] == "explorer"
        assert card["task"] == "find the parser"
        # And the name the agent itself uses. Without it a panel showing three `explorer`
        # cards cannot be matched to the three pieces of work the agent said it split into.
        assert card["handle"] == "parser-finder"


async def test_a_finished_subagent_stays_on_the_list_with_what_it_reported(monkeypatch):
    async with client_app() as (client, app):
        patch_model_resolution(monkeypatch, output_text="found it in parser.py")
        await _launch(app, "parent-1", "find the parser")

        card = (await client.get("/conversations/parent-1/subagents")).json()["subagents"][0]

        # Kept for the life of the thread. The operator's reason for looking is usually
        # that something went wrong, which is exactly when the card is no longer live.
        assert card["status"] == "done"
        assert card["summary"] == "found it in parser.py"
        assert card["ended_at"] is not None


async def test_the_list_is_newest_first(monkeypatch):
    async with client_app() as (client, app):
        patch_model_resolution(monkeypatch, output_text="done")
        await _launch(app, "parent-1", "the first one", "first")
        await _launch(app, "parent-1", "the second one", "second")

        listed = (await client.get("/conversations/parent-1/subagents")).json()
        tasks = [s["task"] for s in listed["subagents"]]
        # The card the operator wants is almost always the most recent, and a list that
        # grew downwards would put it off the bottom of a long session.
        assert tasks == ["the second one", "the first one"]


async def test_a_cards_transcript_opens_through_the_ordinary_conversation_route(
    monkeypatch,
):
    async with client_app() as (client, app):
        patch_model_resolution(monkeypatch, output_text="found it in parser.py")
        started = await _launch(app, "parent-1", "find the parser")

        got = await client.get(f"/conversations/{started.conversation_id}")

        # The claim the panel is built on, and the one that would break silently: a
        # sub-agent's thread is hidden from the session list but is otherwise an ordinary
        # conversation, so the transcript needs no route and no renderer of its own.
        assert got.status_code == 200
        assert [m["role"] for m in got.json()["messages"]] == ["user", "assistant"]


async def test_a_card_carries_the_task_list_the_subagent_wrote_for_itself(monkeypatch):
    """The most legible account of what a long-running sub-agent is actually doing.

    A card showing "running" and a context ring says only that it has not stopped. The
    tasks ride this read rather than a route of their own because the panel already polls
    this one for as long as anything is live, and a per-card fetch would turn one poll into
    one per sub-agent.
    """
    async with client_app() as (client, app):
        patch_model_resolution(monkeypatch, output_text="found it in parser.py")
        started = await _launch(app, "parent-1", "find the parser", "parser-finder")
        await app.state.conversation_tasks.seed(
            "operator", started.conversation_id, ["read parser.py", "check the callers"]
        )

        card = (await client.get("/conversations/parent-1/subagents")).json()["subagents"][0]

        assert [t["content"] for t in card["tasks"]] == [
            "read parser.py",
            "check the callers",
        ]
        # The same shape `GET /conversations/{id}/tasks` serves, because it is that list —
        # so the panel renders it with the component it already has rather than a second.
        assert card["tasks"][0]["status"] == "pending"


async def test_a_subagent_that_wrote_no_tasks_has_an_empty_list_not_a_missing_one(
    monkeypatch,
):
    async with client_app() as (client, app):
        patch_model_resolution(monkeypatch, output_text="found it")
        await _launch(app, "parent-1", "find the parser", "parser-finder")

        card = (await client.get("/conversations/parent-1/subagents")).json()["subagents"][0]

        # Most short sub-agents never write one, and a client that had to tell "no tasks"
        # from "the field is absent" would grow a branch for a case that means nothing.
        assert card["tasks"] == []


async def test_one_threads_subagents_are_not_anothers(monkeypatch):
    async with client_app() as (client, app):
        patch_model_resolution(monkeypatch, output_text="done")
        await _launch(app, "parent-1", "mine")
        await _launch(app, "parent-2", "theirs")

        listed = (await client.get("/conversations/parent-1/subagents")).json()["subagents"]
        assert [s["task"] for s in listed] == ["mine"]
