"""Which model an agent runs on: pins on every source, the operator's choosable list, the
``subagent`` role, and a child inheriting its parent's model."""

from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi import FastAPI
from pydantic_ai.models.test import TestModel

from core.db import init_db, make_engine
from routes.deps import app_state
from runs import Endpoint
from services.agent_models import AgentModels, ChoosableModel, ModelPin
from services.agents import AgentDefinition, builtin_roster
from services.conversations import ConversationBinding, ModelChoice
from services.registry import ModelRegistry, ResolvedModel
from services.settings_store import SettingsStore
from services.subagents import SubagentParent
from services.subagents.models import (
    OfferedModel,
    UnpickableModelError,
    is_local,
    offered,
    pick,
    spawn_model,
)
from tests._helpers import client_app, composer_of, register_stub_provider, stub_resolution
from tests._json import arr, json_body, json_list, obj
from tests.test_subagents_launch import launcher_of, records_of, settle_run

EXPLORER = builtin_roster()["explorer"]
OWNER = "operator"


def _settings() -> AgentModels:
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    return AgentModels(SettingsStore(engine))


type Calls = list[tuple[str, str | None, str | None]]


def _record_resolution(monkeypatch: pytest.MonkeyPatch, subagent_bound: bool = False) -> Calls:
    """Resolve every role to a stub model, recording what each resolution asked for."""
    register_stub_provider(monkeypatch)
    calls: Calls = []

    async def resolve_detailed(
        self: ModelRegistry,
        role: str,
        *,
        owner_id: str,
        override_endpoint_id: str | None = None,
        override_model: str | None = None,
    ) -> ResolvedModel:
        calls.append((role, override_endpoint_id, override_model))
        resolved = await stub_resolution(self, TestModel(custom_output_text="done"))
        endpoint_id = override_endpoint_id or f"{role}-endpoint"
        return replace(
            resolved,
            label=f"{role}:{override_endpoint_id}/{override_model}",
            endpoint=Endpoint(id=endpoint_id, name=endpoint_id, capacity=None),
            model_name=override_model or f"{role}-model",
        )

    real_binding = ModelRegistry.get_role_binding

    async def get_role_binding(
        self: ModelRegistry, owner_id: str, role: str
    ) -> tuple[list[str], str | None]:
        if role == "subagent":
            return (["sub-endpoint"], None) if subagent_bound else ([], None)
        return await real_binding(self, owner_id, role)

    monkeypatch.setattr(ModelRegistry, "resolve_detailed", resolve_detailed)
    monkeypatch.setattr(ModelRegistry, "get_role_binding", get_role_binding)
    return calls


async def _root(app: FastAPI) -> str:
    return await app_state(app).conversations.create_conversation(OWNER, title="root")


class TestSpawnModel:
    def test_a_pin_outranks_everything(self):
        pinned = replace(EXPLORER, endpoint_id="local", model="small")
        chosen = spawn_model(
            pinned,
            ChoosableModel(endpoint_id="cloud", model="big"),
            role_bound=True,
            parent=ModelChoice(endpoint_id="parent"),
        )
        assert (chosen.source, chosen.choice) == (
            "pin",
            ModelChoice(endpoint_id="local", model="small"),
        )

    def test_a_requested_model_comes_next(self):
        chosen = spawn_model(
            EXPLORER,
            ChoosableModel(endpoint_id="cloud", model="big"),
            role_bound=True,
            parent=ModelChoice(),
        )
        assert (chosen.source, chosen.choice) == (
            "spawn",
            ModelChoice(endpoint_id="cloud", model="big"),
        )

    def test_then_the_subagent_role(self):
        chosen = spawn_model(EXPLORER, None, role_bound=True, parent=ModelChoice(model="x"))
        assert (chosen.source, chosen.choice) == ("role", ModelChoice(role="subagent"))

    def test_with_nothing_else_the_parents_model(self):
        parent = ModelChoice(endpoint_id="parent", model="p")
        chosen = spawn_model(EXPLORER, None, role_bound=False, parent=parent)
        assert (chosen.source, chosen.choice) == ("parent", parent)


@pytest.mark.parametrize(
    ("url", "local"),
    [
        ("http://127.0.0.1:8080/v1", True),
        ("http://localhost:1234", True),
        ("http://192.168.1.20:11434/v1", True),
        ("http://studio.local:8000", True),
        ("https://api.anthropic.com", False),
        ("https://8.8.8.8/v1", False),
    ],
)
def test_local_means_this_machine_or_its_network(url: str, local: bool):
    assert is_local(url) is local


def _offer(endpoint: str, model: str) -> OfferedModel:
    entry = ChoosableModel(endpoint_id=endpoint, model=model)
    return OfferedModel(
        id=f"{endpoint}/{model}",
        entry=entry,
        provider="p",
        local=True,
        context_window=None,
        vision=False,
    )


class TestPick:
    def test_an_offered_id_picks_its_entry(self):
        offers = [_offer("local", "small"), _offer("cloud", "big")]
        assert pick(EXPLORER, "cloud/big", offers) == offers[1].entry

    @pytest.mark.parametrize(
        ("agent", "model", "offers", "says"),
        [
            (replace(EXPLORER, model="m"), "local/small", [_offer("local", "small")], "pinned"),
            (EXPLORER, "local/small", None, "unavailable"),
            (EXPLORER, "local/small", [], "listed no models"),
            (EXPLORER, "local/old", [_offer("local", "small")], "local/small"),
        ],
    )
    def test_anything_else_is_refused(
        self,
        agent: AgentDefinition,
        model: str,
        offers: list[OfferedModel] | None,
        says: str,
    ):
        with pytest.raises(UnpickableModelError, match=says):
            _ = pick(agent, model, offers)


class TestSettings:
    async def test_pins_round_trip_and_clear(self):
        models = _settings()
        await models.set_pin(OWNER, "builtin:explorer", ModelPin(endpoint_id="e", model="m"))
        assert (await models.pins(OWNER))["builtin:explorer"] == ModelPin(
            endpoint_id="e", model="m"
        )
        await models.set_pin(OWNER, "builtin:explorer", ModelPin())
        assert await models.pins(OWNER) == {}

    async def test_the_choosable_list_keeps_order_and_drops_duplicates(self):
        models = _settings()
        first = ChoosableModel(endpoint_id="e", model="a", note="file sweeps")
        await models.set_choosable(
            OWNER,
            [
                first,
                ChoosableModel(endpoint_id="e", model="b"),
                first.model_copy(update={"note": ""}),
            ],
        )
        assert [(m.model, m.note) for m in await models.choosable(OWNER)] == [
            ("a", "file sweeps"),
            ("b", ""),
        ]

    async def test_a_corrupt_value_reads_as_nothing(self):
        engine = make_engine("sqlite:///:memory:")
        init_db(engine)
        store = SettingsStore(engine)
        await store.set(OWNER, "agents.choosable_models", "{not json")
        await store.set(OWNER, "agents.model_pins", "[]")
        models = AgentModels(store)
        assert await models.choosable(OWNER) == []
        assert await models.pins(OWNER) == {}


class TestCatalogPins:
    async def test_a_built_in_takes_its_pin(self):
        async with client_app() as (_client, app):
            state = app_state(app)
            await state.agent_models.set_pin(
                OWNER, "builtin:explorer", ModelPin(endpoint_id="e", model="m")
            )
            roster = await state.agents.agents(OWNER)
            assert (roster["explorer"].endpoint_id, roster["explorer"].model) == ("e", "m")
            assert roster["reviewer"].endpoint_id is None

    async def test_the_default_agent_is_never_pinned(self):
        async with client_app() as (_client, app):
            state = app_state(app)
            await state.agent_models.set_pin(OWNER, "builtin:default", ModelPin(endpoint_id="e"))
            roster = await state.agents.agents(OWNER)
            assert roster["default"].endpoint_id is None

    async def test_the_route_pins_a_built_in_and_lists_it_active(self):
        async with client_app() as (client, app):
            registry = app_state(app).models
            endpoint = await registry.create_endpoint(
                OWNER, name="local", base_url="http://127.0.0.1:9/v1", context_window=8000
            )
            response = await client.put(
                "/agents/builtin:explorer/model",
                json={"endpoint_id": endpoint.id, "model": "small"},
            )
            assert response.status_code == 200, response.text
            body = obj(json_body(response))
            assert (body["endpoint_id"], body["model"], body["active"]) == (
                endpoint.id,
                "small",
                True,
            )
            listed = [obj(a) for a in json_list(await client.get("/agents"))]
            explorer = next(a for a in listed if a["id"] == "builtin:explorer")
            assert (explorer["model"], explorer["active"]) == ("small", True)

            cleared = await client.put(
                "/agents/builtin:explorer/model", json={"endpoint_id": None, "model": None}
            )
            assert obj(json_body(cleared))["model"] is None

    async def test_the_route_refuses_an_endpoint_that_cannot_drive_tools(self):
        async with client_app() as (client, app):
            endpoint = await app_state(app).models.create_endpoint(
                OWNER,
                name="chat-only",
                base_url="http://127.0.0.1:9/v1",
                model="m",
                context_window=8000,
                native_tools=False,
            )
            response = await client.put(
                "/agents/builtin:explorer/model", json={"endpoint_id": endpoint.id}
            )
            assert response.status_code == 422

    async def test_the_default_agent_cannot_be_pinned(self):
        async with client_app() as (client, app):
            endpoint = await app_state(app).models.create_endpoint(
                OWNER, name="local", base_url="http://127.0.0.1:9/v1", context_window=8000
            )
            response = await client.put(
                "/agents/builtin:default/model",
                json={"endpoint_id": endpoint.id, "model": "small"},
            )
            assert response.status_code == 422
            assert await app_state(app).agent_models.pins(OWNER) == {}

    async def test_an_endpoint_only_pin_is_stored_with_its_model(self):
        async with client_app() as (client, app):
            endpoint = await app_state(app).models.create_endpoint(
                OWNER,
                name="local",
                base_url="http://127.0.0.1:9/v1",
                model="its-default",
                context_window=8000,
            )
            response = await client.put(
                "/agents/builtin:explorer/model", json={"endpoint_id": endpoint.id}
            )
            assert obj(json_body(response))["model"] == "its-default"


class TestChoosable:
    async def test_offered_drops_a_disabled_endpoint_but_the_setting_keeps_it(self):
        async with client_app() as (client, app):
            registry = app_state(app).models
            local = await registry.create_endpoint(
                OWNER, name="local", base_url="http://127.0.0.1:9/v1", context_window=8000
            )
            cloud = await registry.create_endpoint(
                OWNER, name="cloud", base_url="https://example.invalid/v1", context_window=8000
            )
            saved = await client.put(
                "/models/choosable",
                json={
                    "models": [
                        {"endpoint_id": local.id, "model": "small", "note": "file sweeps"},
                        {"endpoint_id": cloud.id, "model": "big", "note": "hard reviews"},
                    ]
                },
            )
            assert saved.status_code == 200, saved.text
            _ = await registry.update_endpoint(OWNER, cloud.id, enabled=False)

            offers = await offered(registry, app_state(app).agent_models, OWNER)
            assert [(o.id, o.local, o.entry.note) for o in offers] == [
                ("local/small", True, "file sweeps")
            ]
            views = [obj(v) for v in json_list(await client.get("/models/choosable"))]
            assert [(v["model"], v["available"]) for v in views] == [
                ("small", True),
                ("big", False),
            ]

    async def test_a_saved_entry_gone_unavailable_does_not_block_later_saves(self):
        async with client_app() as (client, app):
            registry = app_state(app).models
            local = await registry.create_endpoint(
                OWNER, name="local", base_url="http://127.0.0.1:9/v1", context_window=8000
            )
            cloud = await registry.create_endpoint(
                OWNER, name="cloud", base_url="https://example.invalid/v1", context_window=8000
            )
            gone = {"endpoint_id": cloud.id, "model": "big", "note": ""}
            _ = await client.put("/models/choosable", json={"models": [gone]})
            _ = await registry.update_endpoint(OWNER, cloud.id, enabled=False)

            added = {"endpoint_id": local.id, "model": "small", "note": "sweeps"}
            response = await client.put("/models/choosable", json={"models": [gone, added]})
            assert response.status_code == 200, response.text
            views = [obj(v) for v in json_list(response)]
            assert [(v["model"], v["available"]) for v in views] == [
                ("big", False),
                ("small", True),
            ]

    async def test_saving_refuses_an_unknown_endpoint(self):
        async with client_app() as (client, _app):
            response = await client.put(
                "/models/choosable", json={"models": [{"endpoint_id": "nope", "model": "m"}]}
            )
            assert response.status_code == 404


class TestLaunch:
    async def test_a_child_with_nothing_else_runs_on_its_parents_model(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        calls = _record_resolution(monkeypatch)
        async with client_app() as (client, app):
            root = await _root(app)
            started = await launcher_of(app).launch(
                OWNER,
                EXPLORER,
                "look into it",
                handle="reader",
                parent=SubagentParent(conversation_id=root),
            )
            row = await records_of(app).by_child(started.conversation_id)
            assert row is not None
            assert (row.model_source, row.model_label) == ("parent", "main:None/None")
            binding = await app_state(app).conversations.binding(started.conversation_id)
            assert binding.model_choice == ModelChoice(
                endpoint_id="main-endpoint", model="main-model"
            )
            assert calls[0] == ("main", None, None)
            cards = obj(json_body(await client.get(f"/conversations/{root}/subagents")))
            card = obj(arr(cards["subagents"])[0])
            assert (card["model"], card["model_source"]) == ("main:None/None", "parent")
            await settle_run(app, started.run_id)

    async def test_the_subagent_role_when_bound(self, monkeypatch: pytest.MonkeyPatch):
        calls = _record_resolution(monkeypatch, subagent_bound=True)
        async with client_app() as (_client, app):
            started = await launcher_of(app).launch(
                OWNER,
                EXPLORER,
                "look into it",
                handle="reader",
                parent=SubagentParent(conversation_id=await _root(app)),
            )
            row = await records_of(app).by_child(started.conversation_id)
            assert row is not None and row.model_source == "role"
            assert calls[0] == ("subagent", None, None)
            await settle_run(app, started.run_id)

            # Unbinding the role afterwards doesn't move the child off what it launched on.
            async def unbound(
                self: ModelRegistry, owner_id: str, role: str
            ) -> tuple[list[str], str | None]:
                return [], None

            monkeypatch.setattr(ModelRegistry, "get_role_binding", unbound)
            calls.clear()
            _ = await composer_of(app).resolve(OWNER, started.conversation_id)
            assert calls[0] == ("main", "subagent-endpoint", "subagent-model")

    async def test_a_requested_model_is_fixed_for_the_childs_later_turns(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        calls = _record_resolution(monkeypatch, subagent_bound=True)
        async with client_app() as (_client, app):
            started = await launcher_of(app).launch(
                OWNER,
                EXPLORER,
                "look into it",
                handle="reader",
                parent=SubagentParent(conversation_id=await _root(app)),
                model=ChoosableModel(endpoint_id="local", model="small"),
            )
            await settle_run(app, started.run_id)
            assert calls[0] == ("main", "local", "small")

            calls.clear()
            _ = await composer_of(app).resolve(OWNER, started.conversation_id)
            assert calls[0] == ("main", "local", "small")

    async def test_a_pinned_agent_ignores_a_requested_model(self, monkeypatch: pytest.MonkeyPatch):
        calls = _record_resolution(monkeypatch)
        async with client_app() as (_client, app):
            pinned = replace(EXPLORER, endpoint_id="local", model="small")
            started = await launcher_of(app).launch(
                OWNER,
                pinned,
                "look into it",
                handle="reader",
                parent=SubagentParent(conversation_id=await _root(app)),
                model=ChoosableModel(endpoint_id="cloud", model="big"),
            )
            row = await records_of(app).by_child(started.conversation_id)
            assert row is not None and row.model_source == "pin"
            assert calls[0] == ("main", "local", "small")
            await settle_run(app, started.run_id)

    async def test_a_grandchild_inherits_its_own_parents_model(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        calls = _record_resolution(monkeypatch)
        async with client_app() as (_client, app):
            lead = await app_state(app).agent_store.create(
                OWNER,
                {"name": "lead", "description": "Splits work.", "can_delegate": True},
                published=True,
            )
            parent = await launcher_of(app).launch(
                OWNER,
                lead,
                "split it up",
                handle="planner",
                parent=SubagentParent(conversation_id=await _root(app)),
                model=ChoosableModel(endpoint_id="local", model="small"),
            )
            calls.clear()
            child = await launcher_of(app).launch(
                OWNER,
                EXPLORER,
                "look into it",
                handle="reader",
                parent=SubagentParent(conversation_id=parent.conversation_id),
            )
            row = await records_of(app).by_child(child.conversation_id)
            assert row is not None and row.model_source == "parent"
            assert calls[0] == ("main", "local", "small")
            for started in (child, parent):
                await settle_run(app, started.run_id)


def test_a_binding_without_a_fixed_model_leaves_it_to_the_agent():
    from services.agents.thread import thread_choice

    pinned = replace(EXPLORER, endpoint_id="e", model="m")
    assert thread_choice(ConversationBinding(), pinned) == ModelChoice(endpoint_id="e", model="m")
    fixed = ModelChoice(role="subagent")
    assert thread_choice(ConversationBinding(model_choice=fixed), pinned) == fixed
