"""What the panel reads: a thread's sub-agents, live and finished.

One endpoint, and what these hold is mostly about what it deliberately *isn't*. There is no
sub-agent transcript route, no sub-agent cancel route and no sub-agent approval route,
because a sub-agent is a conversation and a run and those routes already exist — so the
properties worth pinning are the ones that make the card's other links work: every card
carries the ids that reach them, and the list is what survives a restart.
"""

from __future__ import annotations

import asyncio
from typing import cast

import httpx
import pytest
from fastapi import FastAPI
from pydantic import JsonValue

from core.container import ServiceContainer
from runs import RunRegistry
from services.agents import builtin_roster
from services.subagents import LaunchedSubagent, SubagentLauncher
from services.task_list import ConversationTasks
from tests._helpers import app_state, client_app, patch_model_resolution
from tests._json import arr, json_body, obj

EXPLORER = builtin_roster()["explorer"]


def _terminal_tasks(app: FastAPI) -> list[asyncio.Task[None]]:
    return list(cast(set[asyncio.Task[None]], app_state(app, "run_terminal_tasks", set)))


async def _settle(app: FastAPI, run_id: str) -> None:
    run = app_state(app, "runs", RunRegistry).get(run_id)
    assert run is not None
    if run.task is not None:
        await run.task
    for _ in range(5):
        pending = _terminal_tasks(app)
        if not pending:
            await asyncio.sleep(0)
            if not _terminal_tasks(app):
                return
            continue
        _ = await asyncio.gather(*pending, return_exceptions=True)


async def _launch(
    app: FastAPI, conversation_id: str, task: str, handle: str = "the-explorer"
) -> LaunchedSubagent:
    launcher = app_state(app, "capabilities", ServiceContainer).get_optional(SubagentLauncher)
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


async def _first_card(client: httpx.AsyncClient) -> dict[str, JsonValue]:
    listed = json_body(await client.get("/conversations/parent-1/subagents"))
    return obj(arr(listed["subagents"])[0])


async def test_a_thread_with_no_subagents_answers_with_an_empty_list(
    monkeypatch: pytest.MonkeyPatch,
):
    async with client_app() as (client, _app):
        patch_model_resolution(monkeypatch)
        got = await client.get("/conversations/never-delegated/subagents")

        # Not a 404. The panel asks this for every thread the operator opens, and an
        # error for the ordinary case is an error the client has to learn to ignore.
        assert got.status_code == 200
        assert json_body(got) == {"subagents": []}


async def test_each_card_carries_the_ids_its_other_links_need(monkeypatch: pytest.MonkeyPatch):
    async with client_app() as (client, app):
        patch_model_resolution(monkeypatch, output_text="found it in parser.py")
        started = await _launch(app, "parent-1", "find the parser", "parser-finder")

        card = await _first_card(client)

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


async def test_a_finished_subagent_stays_on_the_list_with_what_it_reported(
    monkeypatch: pytest.MonkeyPatch,
):
    async with client_app() as (client, app):
        patch_model_resolution(monkeypatch, output_text="found it in parser.py")
        _ = await _launch(app, "parent-1", "find the parser")

        card = await _first_card(client)

        # Kept for the life of the thread. The operator's reason for looking is usually
        # that something went wrong, which is exactly when the card is no longer live.
        assert card["status"] == "done"
        assert card["summary"] == "found it in parser.py"
        assert card["ended_at"] is not None


async def test_the_list_is_newest_first(monkeypatch: pytest.MonkeyPatch):
    async with client_app() as (client, app):
        patch_model_resolution(monkeypatch, output_text="done")
        _ = await _launch(app, "parent-1", "the first one", "first")
        _ = await _launch(app, "parent-1", "the second one", "second")

        listed = json_body(await client.get("/conversations/parent-1/subagents"))
        tasks = [obj(card)["task"] for card in arr(listed["subagents"])]
        # The card the operator wants is almost always the most recent, and a list that
        # grew downwards would put it off the bottom of a long session.
        assert tasks == ["the second one", "the first one"]


async def test_a_cards_transcript_opens_through_the_ordinary_conversation_route(
    monkeypatch: pytest.MonkeyPatch,
):
    async with client_app() as (client, app):
        patch_model_resolution(monkeypatch, output_text="found it in parser.py")
        started = await _launch(app, "parent-1", "find the parser")

        got = await client.get(f"/conversations/{started.conversation_id}")

        # The claim the panel is built on, and the one that would break silently: a
        # sub-agent's thread is hidden from the session list but is otherwise an ordinary
        # conversation, so the transcript needs no route and no renderer of its own.
        assert got.status_code == 200
        messages = arr(json_body(got)["messages"])
        assert [obj(m)["role"] for m in messages] == ["user", "assistant"]


async def test_a_card_carries_the_task_list_the_subagent_wrote_for_itself(
    monkeypatch: pytest.MonkeyPatch,
):
    """The most legible account of what a long-running sub-agent is actually doing.

    A card showing "running" and a context ring says only that it has not stopped. The
    tasks ride this read rather than a route of their own because the panel already polls
    this one for as long as anything is live, and a per-card fetch would turn one poll into
    one per sub-agent.
    """
    async with client_app() as (client, app):
        patch_model_resolution(monkeypatch, output_text="found it in parser.py")
        started = await _launch(app, "parent-1", "find the parser", "parser-finder")
        _ = await app_state(app, "conversation_tasks", ConversationTasks).seed(
            "operator", started.conversation_id, ["read parser.py", "check the callers"]
        )

        card = await _first_card(client)

        tasks = arr(card["tasks"])
        assert [obj(t)["content"] for t in tasks] == [
            "read parser.py",
            "check the callers",
        ]
        # The same shape `GET /conversations/{id}/tasks` serves, because it is that list —
        # so the panel renders it with the component it already has rather than a second.
        assert obj(tasks[0])["status"] == "pending"


async def test_a_subagent_that_wrote_no_tasks_has_an_empty_list_not_a_missing_one(
    monkeypatch: pytest.MonkeyPatch,
):
    async with client_app() as (client, app):
        patch_model_resolution(monkeypatch, output_text="found it")
        _ = await _launch(app, "parent-1", "find the parser", "parser-finder")

        card = await _first_card(client)

        # Most short sub-agents never write one, and a client that had to tell "no tasks"
        # from "the field is absent" would grow a branch for a case that means nothing.
        assert card["tasks"] == []


async def test_one_threads_subagents_are_not_anothers(monkeypatch: pytest.MonkeyPatch):
    async with client_app() as (client, app):
        patch_model_resolution(monkeypatch, output_text="done")
        _ = await _launch(app, "parent-1", "mine")
        _ = await _launch(app, "parent-2", "theirs")

        listed = arr(json_body(await client.get("/conversations/parent-1/subagents"))["subagents"])
        assert [obj(card)["task"] for card in listed] == ["mine"]
