"""Agent trees: delegation below depth one, messages between agents, stopping, and opening
a spawned agent as a thread of its own."""

from __future__ import annotations

import pytest
from fastapi import FastAPI

from agent.injected import injected_text
from harness.manifests._subagent_wake import WAKE_CHAIN_LIMIT, SubagentWake
from runs import RunRegistry
from runs.events import now_utc
from runs.run import QueuedMessage
from services.agents import AgentDefinition, AgentStore, builtin_roster, lower_of
from services.conversations import ConversationStore
from services.subagents import LaunchedSubagent, SubagentParent, SubagentUnavailableError
from services.subagents.report import message_body, message_envelope
from tests._helpers import app_state, client_app, patch_model_resolution
from tests._json import arr, json_body, obj, parse_json
from tests.test_subagents_launch import _launcher, _records, _settle

EXPLORER = builtin_roster()["explorer"]


def _conversations(app: FastAPI) -> ConversationStore:
    return app_state(app, "conversations", ConversationStore)


def _runs(app: FastAPI) -> RunRegistry:
    return app_state(app, "runs", RunRegistry)


async def _root(app: FastAPI) -> str:
    return await _conversations(app).create_conversation("operator", title="root")


async def _lead(app: FastAPI, *, max_depth: int | None = None) -> AgentDefinition:
    """A published agent that may delegate."""
    values: dict[str, object] = {
        "name": "lead",
        "description": "Splits work up.",
        "instructions": "Delegate.",
        "can_delegate": True,
        "max_depth": max_depth,
        "permission_ceiling": "plan",
    }
    return await app_state(app, "agent_store", AgentStore).create(
        "operator", values, published=True
    )


async def _spawn(
    app: FastAPI, agent: AgentDefinition, handle: str, parent: str
) -> LaunchedSubagent:
    return await _launcher(app).launch(
        "operator",
        agent,
        "look into it",
        handle=handle,
        parent=SubagentParent(conversation_id=parent),
    )


def _wake(app: FastAPI) -> SubagentWake:
    return app_state(app, "subagent_wake", SubagentWake)


async def _settle_thread(app: FastAPI, conversation_id: str) -> None:
    run = _runs(app).active_run_for(conversation_id, "operator")
    if run is not None:
        await _settle(app, run.id)


class TestDepth:
    async def test_an_agent_that_may_delegate_spawns_below_itself(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="done")
            root = await _root(app)
            lead = await _spawn(app, await _lead(app), "planner", root)
            child = await _spawn(app, EXPLORER, "reader", lead.conversation_id)

            row = await _records(app).by_child(child.conversation_id)
            assert row is not None
            assert (row.depth, row.root_conversation_id) == (2, root)
            await _settle(app, child.run_id)
            await _settle(app, lead.run_id)

    async def test_a_definition_lowers_how_deep_its_tree_may_grow(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="done")
            root = await _root(app)
            lead = await _spawn(app, await _lead(app, max_depth=1), "planner", root)

            with pytest.raises(SubagentUnavailableError, match="as deep"):
                _ = await _spawn(app, EXPLORER, "reader", lead.conversation_id)
            await _settle(app, lead.run_id)

    async def test_an_agent_that_may_not_delegate_is_refused(self, monkeypatch: pytest.MonkeyPatch):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="done")
            root = await _root(app)
            explorer = await _spawn(app, EXPLORER, "reader", root)
            await _conversations(app).set_agent_name(explorer.conversation_id, "explorer")

            with pytest.raises(SubagentUnavailableError, match="not allowed to delegate"):
                _ = await _spawn(app, EXPLORER, "sub-reader", explorer.conversation_id)
            await _settle(app, explorer.run_id)

    async def test_handles_are_unique_across_the_tree(self, monkeypatch: pytest.MonkeyPatch):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="done")
            root = await _root(app)
            lead = await _spawn(app, await _lead(app), "planner", root)
            first = await _spawn(app, EXPLORER, "reader", root)
            second = await _spawn(app, EXPLORER, "reader", lead.conversation_id)

            assert (first.handle, second.handle) == ("reader", "reader-2")
            for started in (first, second, lead):
                await _settle(app, started.run_id)

    async def test_an_address_is_never_a_handle(self, monkeypatch: pytest.MonkeyPatch):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="done")
            started = await _spawn(app, EXPLORER, "parent", await _root(app))
            assert started.handle == "parent-2"
            await _settle(app, started.run_id)


class TestMessaging:
    async def test_a_message_queues_onto_a_running_agent_with_its_sender(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="done")
            root = await _root(app)
            started = await _spawn(app, EXPLORER, "reader", root)
            run = _runs(app).get(started.run_id)
            assert run is not None

            label = await _launcher(app).message("operator", root, "reader", "only the parser")

            assert label == "reader"
            queued = run.pending_messages[-1]
            assert (queued.source, queued.sender) == ("agent", "main")
            await _settle(app, started.run_id)

    async def test_a_sibling_is_reachable_by_its_handle(self, monkeypatch: pytest.MonkeyPatch):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="done")
            root = await _root(app)
            lexer = await _spawn(app, EXPLORER, "lexer", root)
            parser = await _spawn(app, EXPLORER, "parser", root)
            run = _runs(app).get(parser.run_id)
            assert run is not None

            _ = await _launcher(app).message("operator", lexer.conversation_id, "parser", "tokens")

            assert run.pending_messages[-1].sender == "lexer"
            await _settle(app, lexer.run_id)
            await _settle(app, parser.run_id)

    async def test_an_idle_agent_is_woken_and_marked_working(self, monkeypatch: pytest.MonkeyPatch):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="done")
            root = await _root(app)
            started = await _spawn(app, EXPLORER, "reader", root)
            await _settle(app, started.run_id)
            await _settle_thread(app, root)

            _ = await _launcher(app).message("operator", root, "reader", "one more thing")

            woken = _runs(app).active_run_for(started.conversation_id, "operator")
            assert woken is not None
            assert woken.kind == "wake"
            row = await _records(app).by_child(started.conversation_id)
            assert row is not None
            assert row.run_id == woken.id
            await _settle(app, woken.id)
            turns = await _conversations(app).messages_view(started.conversation_id)
            assert any(t.role == "subagent" and t.author == "main" for t in turns)
            await _settle_thread(app, root)

    async def test_a_child_reaches_the_thread_that_spawned_it(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="done")
            root = await _root(app)
            started = await _spawn(app, EXPLORER, "reader", root)

            label = await _launcher(app).message(
                "operator", started.conversation_id, "parent", "found it early"
            )

            assert label == "main"
            assert _runs(app).active_run_for(root, "operator") is not None
            await _settle(app, started.run_id)
            await _settle_thread(app, root)

    async def test_a_recipient_is_upward_only_when_it_is_above_the_sender(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="done")
            root = await _root(app)
            lead = await _spawn(app, await _lead(app), "planner", root)
            reader = await _spawn(app, EXPLORER, "reader", lead.conversation_id)
            sibling = await _spawn(app, EXPLORER, "lexer", lead.conversation_id)
            launcher = _launcher(app)

            parent = await launcher.recipient("operator", reader.conversation_id, "parent")
            above = await launcher.recipient("operator", reader.conversation_id, "planner")
            top = await launcher.recipient("operator", reader.conversation_id, "main")
            across = await launcher.recipient("operator", reader.conversation_id, "lexer")
            down = await launcher.recipient("operator", lead.conversation_id, "reader")

            assert (parent.label, parent.agent_name, parent.upward) == ("planner", "lead", True)
            assert above.upward and top.upward
            assert (across.agent_name, across.upward) == ("explorer", False)
            assert not down.upward
            for started in (reader, sibling, lead):
                await _settle(app, started.run_id)

    async def test_the_root_has_no_parent(self, monkeypatch: pytest.MonkeyPatch):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch)
            with pytest.raises(SubagentUnavailableError, match="no parent"):
                _ = await _launcher(app).message("operator", await _root(app), "parent", "hello")

    async def test_another_trees_agent_is_out_of_reach(self, monkeypatch: pytest.MonkeyPatch):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="done")
            started = await _spawn(app, EXPLORER, "reader", await _root(app))

            with pytest.raises(SubagentUnavailableError, match="reader"):
                _ = await _launcher(app).message("operator", await _root(app), "reader", "hi")
            await _settle(app, started.run_id)

    async def test_a_stopped_agent_is_not_woken(self, monkeypatch: pytest.MonkeyPatch):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="done")
            root = await _root(app)
            started = await _spawn(app, EXPLORER, "reader", root)
            _ = await _launcher(app).stop("operator", root, "reader")
            await _settle(app, started.run_id)

            with pytest.raises(SubagentUnavailableError, match="stopped"):
                _ = await _launcher(app).message("operator", root, "reader", "still there?")
            await _settle_thread(app, root)


class TestStopping:
    async def test_stopping_an_agent_stops_everything_below_it(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="done")
            root = await _root(app)
            lead = await _spawn(app, await _lead(app), "planner", root)
            child = await _spawn(app, EXPLORER, "reader", lead.conversation_id)

            stopped = await _launcher(app).stop("operator", root, "planner")

            assert set(stopped) <= {"planner", "reader"}
            await _settle(app, child.run_id)
            await _settle(app, lead.run_id)
            for started in (lead, child):
                row = await _records(app).by_child(started.conversation_id)
                assert row is not None
                assert row.status == "cancelled"
            await _settle_thread(app, root)

    async def test_a_sibling_is_not_yours_to_stop(self, monkeypatch: pytest.MonkeyPatch):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="done")
            root = await _root(app)
            lexer = await _spawn(app, EXPLORER, "lexer", root)
            parser = await _spawn(app, EXPLORER, "parser", root)

            with pytest.raises(SubagentUnavailableError, match="not yours to stop"):
                _ = await _launcher(app).stop("operator", lexer.conversation_id, "parser")
            await _settle(app, lexer.run_id)
            await _settle(app, parser.run_id)


class TestTheTreesWakeBudget:
    async def test_a_tree_at_its_budget_stops_waking_its_agents(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="done")
            root = await _root(app)
            started = await _spawn(app, EXPLORER, "reader", root)
            await _settle(app, started.run_id)
            await _settle_thread(app, root)
            _wake(app)._chain[root] = WAKE_CHAIN_LIMIT  # noqa: SLF001

            _ = await _launcher(app).message("operator", root, "reader", "again")

            assert _runs(app).active_run_for(started.conversation_id, "operator") is None

    async def test_the_operator_writing_to_an_opened_agent_clears_its_trees_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="done")
            root = await _root(app)
            started = await _spawn(app, EXPLORER, "reader", root)
            await _settle(app, started.run_id)
            wake = _wake(app)
            wake._chain[root] = WAKE_CHAIN_LIMIT  # noqa: SLF001
            run = _runs(app).get(started.run_id)
            assert run is not None
            run.kind = "chat"

            wake.observed(run)

            assert root not in wake._chain  # noqa: SLF001
            await _settle_thread(app, root)


class TestOpeningAsAThread:
    async def test_an_opened_agent_is_listed_under_its_parent(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        async with client_app() as (client, app):
            patch_model_resolution(monkeypatch, output_text="done")
            root = await _root(app)
            started = await _spawn(app, EXPLORER, "reader", root)
            await _settle(app, started.run_id)
            await _settle_thread(app, root)

            opened = await client.post(f"/conversations/{started.conversation_id}/open")

            assert opened.status_code == 200
            assert json_body(opened) == {"id": started.conversation_id, "parent_id": root}
            listed = parse_json((await client.get("/conversations")).content)
            rows = arr(obj(listed)["conversations"]) if isinstance(listed, dict) else arr(listed)
            row = next(obj(c) for c in rows if obj(c)["id"] == started.conversation_id)
            assert row["parent_id"] == root
            cards = arr(
                json_body(await client.get(f"/conversations/{root}/subagents"))["subagents"]
            )
            assert obj(cards[0])["opened"] is True

    async def test_a_thread_nobody_spawned_cannot_be_opened(self, monkeypatch: pytest.MonkeyPatch):
        async with client_app() as (client, app):
            patch_model_resolution(monkeypatch)
            got = await client.post(f"/conversations/{await _root(app)}/open")
            assert got.status_code == 404

    async def test_the_panel_shows_the_whole_subtree(self, monkeypatch: pytest.MonkeyPatch):
        async with client_app() as (client, app):
            patch_model_resolution(monkeypatch, output_text="done")
            root = await _root(app)
            lead = await _spawn(app, await _lead(app), "planner", root)
            child = await _spawn(app, EXPLORER, "reader", lead.conversation_id)
            await _settle(app, child.run_id)
            await _settle(app, lead.run_id)

            cards = arr(
                json_body(await client.get(f"/conversations/{root}/subagents"))["subagents"]
            )

            by_handle = {obj(card)["handle"]: obj(card) for card in cards}
            assert by_handle["reader"]["parent_conversation_id"] == lead.conversation_id
            assert by_handle["reader"]["depth"] == 2
            assert by_handle["planner"]["parent_conversation_id"] == root
            await _settle_thread(app, lead.conversation_id)
            await _settle_thread(app, root)


class TestHowAMessageReads:
    def test_it_is_framed_with_its_sender_and_not_as_the_operator(self):
        text = injected_text(
            QueuedMessage(
                id="m1", text="tokens", queued_at=now_utc(), source="agent", sender="lexer"
            )
        )
        assert "not from the operator" in text
        assert message_body(text) == ("lexer", "tokens")

    def test_an_opening_tag_alone_is_not_an_envelope(self):
        assert message_body("<agent-message>\nFrom `x`:\nhi\n</agent-message>") is None

    def test_a_round_trip_keeps_the_text(self):
        assert message_body(message_envelope("a", " two\nlines ")) == ("a", "two\nlines")


class TestASpecAsksRatherThanGrants:
    """Numeric fields fold under what the operator allowed; a definition never overrules."""

    def test_an_operator_who_lowered_the_ceiling_is_not_overruled(self):
        assert lower_of(60, 10) == 10

    def test_a_spec_may_still_raise_a_default_nobody_chose(self):
        assert lower_of(60, None) == 60

    def test_a_spec_that_asks_for_nothing_takes_the_bound(self):
        assert lower_of(None, 10) == 10
        assert lower_of(None, None) is None


class TestHowAReportNamesItself:
    def test_it_is_named_by_the_handle_with_the_spec_beside_it(self):
        from harness.manifests._subagent_wake import _report_text

        text = _report_text("helper-function-finder", "explorer", "found it", None, None)
        assert text.startswith("Sub-agent `helper-function-finder` (explorer) finished.")

    def test_a_sub_agent_named_after_its_spec_is_not_named_twice(self):
        from harness.manifests._subagent_wake import _report_text

        text = _report_text("explorer", "explorer", "found it", None, None)
        assert text.startswith("Sub-agent `explorer` finished.")
