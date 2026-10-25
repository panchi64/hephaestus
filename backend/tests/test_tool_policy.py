"""The operator disabling individual tools, end to end.

The gate itself is in ``test_tools.py``. This covers the persisted set, the catalog, and
that the set reaches every path a turn is composed on, composing with offline mode's web
suspension rather than replacing it.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import cast, final, override

import pytest
from fastapi import FastAPI
from pydantic_ai import RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.toolsets import AbstractToolset
from pydantic_ai.usage import RunUsage

import harness.turns as harness_turns
import routes.chat.submit as chat_routes
import routes.runs as runs_routes
from agent import build_resume_orchestrator
from core.db import init_db, make_engine
from routes.chat.turn import compose_turn
from routes.deps import app_state
from runs import Run, RunStream
from services.registry import ModelRegistry
from services.settings_store import DISABLED_TOOLS_KEY, SettingsStore
from services.tool_policy import (
    ATTENDED_ONLY_TOOLS,
    VISION_ONLY_TOOLS,
    CategoryAvailability,
    WebToolsGate,
    effective_disabled_tools,
    get_disabled_tools,
    lane_disabled_tools,
    set_tool_enabled,
    unavailable_tools,
    vision_disabled_tools,
)
from tools import RunDeps, build_agent_toolsets
from tools.catalog import tool_catalog

from ._helpers import (
    client_app,
    full_tool_categories,
    parked_turn,
    patch_model_resolution,
    register_stub_provider,
    stub_resolution,
    swap_tool_catalog,
)
from ._json import json_body, json_list, obj, s
from .command_workspace import command_workspace
from .test_approval_routes import danger_categories as _approval_danger_categories
from .test_task_routes import await_task_run_finalized, create_task, parked_call_id

OWNER = "operator"


def make_store() -> SettingsStore:
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    return SettingsStore(engine)


def _registered(app: FastAPI) -> dict[str, AbstractToolset[RunDeps]]:
    return app_state(app).tool_categories


def _gated(app: FastAPI) -> frozenset[str]:
    return app_state(app).gated_tools


def _availability(app: FastAPI) -> tuple[CategoryAvailability, ...]:
    return app_state(app).category_availability


async def agent_visible(disabled: frozenset[str]) -> set[str]:
    """Every tool name the composed stack can resolve, deferred ones included.

    Which a request actually carries is ``test_tool_search.py``'s subject."""
    run = Run(id="t", kind="chat", owner_id=OWNER, stream=RunStream())
    # A spawned thread is also offered `subagent_report`.
    deps = RunDeps(run=run, owner_id=OWNER, disabled_tools=disabled, reports_to_parent=True)
    ctx = RunContext(deps=deps, workspace=command_workspace(), model=TestModel(), usage=RunUsage())
    return set(await build_agent_toolsets(full_tool_categories())[0].get_tools(ctx))


async def test_catalog_matches_the_names_the_agent_is_offered():
    # Pins the `category_tool` namespacing against Pydantic AI's actual prefixing, so a
    # library change cannot silently detach the settings toggles.
    offered = await agent_visible(frozenset())
    assert {t.name for t in tool_catalog(full_tool_categories())} == offered


# The full catalog, frozen: catches a manifest that drops or double-claims a category.
_PINNED_CATALOG = {
    "agents_discard",
    "agents_list",
    "agents_merge",
    "agents_message",
    "agents_publish",
    "agents_read",
    "agents_spawn",
    "agents_stop",
    "agents_write",
    "subagent_report",
    "attachments_provision",
    "browse_click",
    "browse_console_messages",
    "browse_execute_js",
    "browse_get_text",
    "browse_go_back",
    "browse_go_forward",
    "browse_handle_next_dialog",
    "browse_hover",
    "browse_navigate",
    "browse_network_requests",
    "browse_press_key",
    "browse_screenshot",
    "browse_scroll",
    "browse_select_option",
    "browse_snapshot",
    "browse_tabs",
    "browse_type_text",
    "browse_wait_for",
    "builtin_ask_user",
    "builtin_now",
    "calendar_agenda",
    "calendar_create_event",
    "calendar_delete_event",
    "calendar_list_calendars",
    "calendar_update_event",
    "code_execute",
    "code_request_egress",
    "shell_run_on_host",
    "corpus_retrieve",
    "files_create_directory",
    "files_edit_file",
    "files_file_info",
    "files_find_files",
    "files_list_directory",
    "files_read_file",
    "files_search_files",
    "files_write_file",
    "mail_draft_reply",
    "mail_list_accounts",
    "mail_list_messages",
    "mail_mark",
    "mail_read",
    "mail_reply",
    "mail_send",
    "memory_audit",
    "memory_consolidate",
    "memory_forget",
    "memory_list",
    "memory_open",
    "memory_recall",
    "memory_remember",
    "memory_update",
    "plan_enter",
    "plan_read",
    "plan_submit",
    "platform_read",
    "platform_search",
    "plugins_read",
    "plugins_test",
    "plugins_write",
    "project_active",
    "project_list",
    "project_open_worktree",
    "routines_delete",
    "routines_list",
    "routines_run_now",
    "routines_write",
    "shell_check_command",
    "shell_run_command",
    "shell_start_command",
    "shell_stop_command",
    "skills_create",
    "skills_edit",
    "skills_open",
    "tasks_read",
    "tasks_update_statuses",
    "tasks_write",
    "vault_get_entry",
    "vault_list_entries",
    "view_close",
    "view_show",
    "web_fetch",
    "web_search",
}


async def test_assembled_catalog_is_pinned():
    assert {t.name for t in tool_catalog(full_tool_categories())} == _PINNED_CATALOG


async def test_booted_app_assembles_the_same_catalog():
    # The discovery-based helper and the real app must assemble identically, or the pin
    # guards a mapping no run uses.
    async with client_app() as (_, app):
        assert {t.name for t in tool_catalog(_registered(app))} == _PINNED_CATALOG
        # Each manifest's gated names plus `tools.CORE_GATED_TOOLS`; a name missing here
        # parks the run but can never be granted for the conversation.
        assert _gated(app) == {
            "platform_search",
            "plan_submit",
            "agents_write",
            "shell_run_command",
            "shell_start_command",
            "files_write_file",
            "files_edit_file",
            "files_create_directory",
            "plugins_test",
            "routines_write",
            "routines_delete",
            "memory_forget",
            "memory_consolidate",
        }
        assert _gated(app) <= _PINNED_CATALOG


async def test_catalog_carries_category_and_description():
    now = next(t for t in tool_catalog(full_tool_categories()) if t.name == "builtin_now")
    assert now.category == "builtin"
    assert "offset from UTC" in now.description


async def test_operator_disabled_tool_is_absent_from_the_catalog_the_agent_sees():
    store = make_store()
    _ = await set_tool_enabled(store, OWNER, "builtin_now", False)
    disabled = await get_disabled_tools(store, OWNER)
    visible = await agent_visible(disabled)
    assert "builtin_now" not in visible
    assert "attachments_provision" in visible


async def test_flip_round_trips_and_persists():
    store = make_store()
    _ = await set_tool_enabled(store, OWNER, "memory_recall", False)
    assert await get_disabled_tools(store, OWNER) == {"memory_recall"}
    _ = await set_tool_enabled(store, OWNER, "memory_recall", True)
    assert await get_disabled_tools(store, OWNER) == frozenset()


async def test_a_corrupt_stored_value_reads_as_nothing_disabled():
    # Fail open: a mangled preference must not take the whole catalog away from the agent.
    store = make_store()
    await store.set(OWNER, DISABLED_TOOLS_KEY, "{not json")
    assert await get_disabled_tools(store, OWNER) == frozenset()
    await store.set(OWNER, DISABLED_TOOLS_KEY, json.dumps({"memory_recall": True}))
    assert await get_disabled_tools(store, OWNER) == frozenset()


@final
class _StubOffline(WebToolsGate):
    def __init__(self, disabled: frozenset[str]) -> None:
        self._disabled: frozenset[str] = disabled

    @override
    def web_tools_disabled(self) -> frozenset[str]:
        return self._disabled


def stub_offline(disabled: frozenset[str]) -> _StubOffline:
    """Offline mode for `effective_disabled_tools`; the real service needs a live monitor."""
    return _StubOffline(disabled)


async def test_operator_and_offline_sets_union_rather_than_overwrite():
    store = make_store()
    _ = await set_tool_enabled(store, OWNER, "builtin_now", False)
    offline = stub_offline(frozenset({"web_search", "web_fetch"}))
    disabled = await effective_disabled_tools(store, offline, OWNER)
    assert disabled == {
        "builtin_now",
        "web_search",
        "web_fetch",
    }


async def test_offline_alone_still_applies_with_no_operator_choices():
    store = make_store()
    offline = stub_offline(frozenset({"web_search"}))
    disabled = await effective_disabled_tools(store, offline, OWNER)
    assert disabled == {"web_search"}


async def test_a_model_that_cannot_see_is_not_offered_an_image_tool():
    store = make_store()
    offline = stub_offline(frozenset())
    blind = await effective_disabled_tools(store, offline, OWNER, vision=False)
    sighted = await effective_disabled_tools(store, offline, OWNER, vision=True)
    assert "browse_screenshot" in blind
    assert "browse_screenshot" not in sighted
    assert blind - sighted == {"browse_screenshot"}


def test_the_vision_gate_names_tools_that_actually_exist():
    # The set lives in `services/`, below `tools/`, so a rename would leave it covering nothing.
    assert VISION_ONLY_TOOLS <= _PINNED_CATALOG


async def test_the_dormant_groups_name_categories_the_catalog_really_has():
    # A dormant declaration names a category, so a rename would pass the boot check with a
    # group that no longer holds the advertised tools.
    async with client_app() as (_, app):
        dormant = app_state(app).dormant_categories
        for entry in dormant:
            assert any(name.startswith(f"{entry.category}_") for name in _PINNED_CATALOG)


def test_the_lane_gate_names_tools_that_actually_exist():
    # A stale name would offer a scheduled task a tool that parks the turn with no operator
    # present, and a parked turn waits until the process restarts.
    assert ATTENDED_ONLY_TOOLS <= _PINNED_CATALOG
    assert lane_disabled_tools("task") <= _PINNED_CATALOG


async def _unconfigured(app: FastAPI) -> frozenset[str]:
    """What a fresh test app withholds for lack of a mailbox, calendar or vault."""
    return await unavailable_tools(_availability(app), app_state(app).capabilities, OWNER)


def _force_offline(monkeypatch: pytest.MonkeyPatch, app: FastAPI, *names: str) -> None:
    """Pin the live offline service's automatic set; the lifespan still owns its shutdown."""
    monkeypatch.setattr(
        app_state(app).offline,
        "web_tools_disabled",
        lambda: frozenset(names),
    )


def spy_disabled_tools[**P, R](
    real: Callable[P, R], captured: dict[str, frozenset[str]]
) -> Callable[P, R]:
    """``real``, recording the ``disabled_tools`` keyword each call is composed with."""

    def spy(*args: P.args, **kwargs: P.kwargs) -> R:
        captured["disabled"] = cast(frozenset[str], kwargs["disabled_tools"])
        return real(*args, **kwargs)

    return spy


async def test_route_lists_the_catalog_with_enabled_state():
    async with client_app() as (client, app):
        rows = [obj(r) for r in json_list(await client.get("/tools"))]
        expected = tool_catalog(_registered(app))
        assert {s(r["name"]) for r in rows} == {t.name for t in expected}
        assert all(r["enabled"] for r in rows)

        resp = await client.put("/tools/builtin_now", json={"enabled": False})
        assert resp.status_code == 200
        assert json_body(resp)["enabled"] is False

        rows = [obj(r) for r in json_list(await client.get("/tools"))]
        assert next(r for r in rows if r["name"] == "builtin_now")["enabled"] is False
        assert next(r for r in rows if r["name"] == "memory_recall")["enabled"] is True


async def test_route_rejects_an_unknown_tool_name():
    async with client_app() as (client, _app):
        resp = await client.put("/tools/not_a_tool", json={"enabled": False})
        assert resp.status_code == 404


async def test_chat_turn_composes_with_the_operator_set(monkeypatch: pytest.MonkeyPatch):
    async with client_app() as (client, app):
        _ = await client.put("/tools/builtin_now", json={"enabled": False})
        _force_offline(monkeypatch, app, "web_search")

        captured: dict[str, frozenset[str]] = {}
        monkeypatch.setattr(chat_routes, "compose_turn", spy_disabled_tools(compose_turn, captured))
        patch_model_resolution(monkeypatch)
        _ = await client.post("/chat", json={"prompt": "hi"})
        unconfigured = await _unconfigured(app)

    # The stub model declares no vision; subtract the automatic sources.
    automatic = vision_disabled_tools(False) | unconfigured
    assert captured["disabled"] - automatic == {
        "builtin_now",
        "web_search",
    }


async def test_unattended_task_turn_composes_with_the_operator_set_and_the_model(
    monkeypatch: pytest.MonkeyPatch,
):
    """An unattended turn resolves its own model, so it must apply the vision gate too."""
    patch_model_resolution(monkeypatch, output_text="done")
    async with client_app() as (client, _app):
        _ = await client.put("/tools/builtin_now", json={"enabled": False})

        captured: dict[str, frozenset[str]] = {}
        monkeypatch.setattr(
            harness_turns, "compose_turn", spy_disabled_tools(compose_turn, captured)
        )
        created = await create_task(client)
        fired = await client.post(f"/tasks/{created['id']}/run_now")
        _ = await await_task_run_finalized(
            client, s(created["id"]), s(json_body(fired)["taskRunId"])
        )

    assert VISION_ONLY_TOOLS <= captured["disabled"]
    assert "builtin_now" in captured["disabled"]


def _install_sensitive_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    """A TestModel plus one approval-required tool, so a turn parks. Pair with
    ``swap_tool_catalog(app, _danger_categories())`` after boot."""

    async def fake_resolve_detailed(self: ModelRegistry, _role: str, **_kwargs: object):
        return await stub_resolution(self, TestModel(custom_output_text="done"))

    register_stub_provider(monkeypatch)
    monkeypatch.setattr(ModelRegistry, "resolve_detailed", fake_resolve_detailed)


def _danger_categories():
    """The approval fixture's catalog plus one ungated tool to switch off while parked."""
    categories = _approval_danger_categories()

    # Declares `read`; undeclared, the level would gate this tool too and park on two approvals.
    @categories["danger"].tool_plain(metadata={"sensitivity": "read"})
    def safe_thing() -> str:
        return "safe"

    return categories


async def _await_parked(app: FastAPI, run_id: str) -> Run:
    for _ in range(200):
        run = app_state(app).runs.get(run_id)
        if run is not None and run.status == "awaiting_input":
            return run
        await asyncio.sleep(0.01)
    raise AssertionError("run never parked")


async def test_resume_applies_the_operator_set_and_the_offline_set(
    monkeypatch: pytest.MonkeyPatch,
):
    """Both sources reach the resumed turn's deps, including a tool switched off while parked."""
    _install_sensitive_tool(monkeypatch)
    async with client_app() as (client, app):
        swap_tool_catalog(app, _danger_categories())
        run_id = s(json_body(await client.post("/chat", json={"prompt": "delete it"}))["run_id"])
        run = await _await_parked(app, run_id)
        compaction = parked_turn(run).compaction
        assert compaction is not None
        turn_start_withheld = compaction.deps.disabled_tools

        # Switched off after the park: resume must use the current policy, not the one at run start.
        _ = await client.put("/tools/danger_safe_thing", json={"enabled": False})
        _force_offline(monkeypatch, app, "web_search")

        captured: dict[str, frozenset[str]] = {}
        monkeypatch.setattr(
            runs_routes,
            "build_resume_orchestrator",
            spy_disabled_tools(build_resume_orchestrator, captured),
        )

        call_id = parked_call_id(run)
        resp = await client.post(
            f"/runs/{run_id}/approve",
            json={"decisions": [{"tool_call_id": call_id, "approved": True}]},
        )
        assert resp.status_code == 202
        unconfigured = await _unconfigured(app)

    assert captured["disabled"] - unconfigured == {"danger_safe_thing", "web_search"} | (
        turn_start_withheld - unconfigured
    )
