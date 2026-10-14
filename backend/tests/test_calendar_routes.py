"""The `/calendar` REST surface (`CAL-1..3`) and the agent's calendar tools."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from pydantic import JsonValue
from pydantic_ai import ModelMessage, ModelResponse, RunContext, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage
from sqlalchemy import Engine

from core.container import ServiceContainer
from core.vault import Vault
from runs import Run, RunStream
from services.calendar import CalendarService
from tools import RunDeps
from tools.calendar import calendar_toolset

from ._helpers import app_state, client_app, register_stub_provider, stub_resolution
from ._json import JSON, arr, json_body, obj, s


async def _calendar(client: httpx.AsyncClient, name: str = "Personal", **extra: str) -> str:
    resp = await client.post("/calendar/calendars", json={"name": name, **extra})
    assert resp.status_code == 201, resp.text
    return s(json_body(resp)["id"])


# --- calendars ------------------------------------------------------------


async def test_calendar_crud_round_trip():
    async with client_app() as (client, _app):
        created = await client.post("/calendar/calendars", json={"name": "Work", "tone": "info"})
        assert created.status_code == 201
        calendar_id = s(json_body(created)["id"])
        assert json_body(created)["synced"] is False

        listed = await client.get("/calendar/calendars")
        assert [obj(row)["name"] for row in arr(json_body(listed)["items"])] == ["Work"]

        patched = await client.patch(
            f"/calendar/calendars/{calendar_id}", json={"name": "Work (renamed)"}
        )
        assert patched.json()["name"] == "Work (renamed)"

        assert (await client.delete(f"/calendar/calendars/{calendar_id}")).status_code == 204
        assert (await client.get("/calendar/calendars")).json()["items"] == []


async def test_an_empty_calendar_name_is_rejected():
    async with client_app() as (client, _app):
        resp = await client.post("/calendar/calendars", json={"name": "   "})
        assert resp.status_code == 422


async def test_an_unknown_calendar_is_a_404():
    async with client_app() as (client, _app):
        assert (await client.patch("/calendar/calendars/nope", json={})).status_code == 404


async def test_binding_a_calendar_to_a_server_never_echoes_the_password():
    async with client_app() as (client, _app):
        resp = await client.post(
            "/calendar/calendars",
            json={
                "name": "Remote",
                "caldavUrl": "https://dav.example.com/cal/",
                "caldavUsername": "operator",
                "caldavPassword": "hunter2",
            },
        )
        body = resp.text
        assert resp.status_code == 201
        assert resp.json()["synced"] is True
        assert resp.json()["syncUrl"] == "https://dav.example.com/cal/"
        assert "hunter2" not in body and "caldavPassword" not in body


async def test_syncing_a_local_calendar_is_a_422():
    async with client_app() as (client, _app):
        calendar_id = await _calendar(client)
        resp = await client.post(f"/calendar/calendars/{calendar_id}/sync")
        assert resp.status_code == 422


async def test_syncing_a_private_server_is_refused_as_an_upstream_error():
    async with client_app() as (client, _app):
        calendar_id = await _calendar(
            client, "LAN", caldavUrl="http://127.0.0.1:8080/dav/", caldavUsername="operator"
        )
        resp = await client.post(f"/calendar/calendars/{calendar_id}/sync")
        assert resp.status_code == 502


async def test_syncing_everything_with_nothing_bound_reports_zero_calendars():
    """`calendars: 0` is a different answer to "no changes" — a caller must be able to say
    "there is nothing to sync" rather than "everything is up to date"."""
    async with client_app() as (client, _app):
        _ = await _calendar(client)  # local-only, so not remote
        resp = await client.post("/calendar/sync")
        assert resp.status_code == 200
        assert resp.json() == {"calendars": 0, "changed": 0, "failed": []}


async def test_syncing_everything_reports_a_failed_calendar_without_abandoning_the_rest():
    """One unreachable server must not stop the others: the whole pass still succeeds and
    names what failed, so a stale binding can't silently freeze the rest of the schedule."""
    async with client_app() as (client, _app):
        _ = await _calendar(client, "Local")
        _ = await _calendar(
            client, "LAN", caldavUrl="http://127.0.0.1:8080/dav/", caldavUsername="operator"
        )
        resp = await client.post("/calendar/sync")
        assert resp.status_code == 200
        body = json_body(resp)
        assert body["calendars"] == 1
        assert body["changed"] == 0
        assert body["failed"] == ["LAN"]


# --- events + occurrences --------------------------------------------------


async def test_event_crud_round_trip():
    async with client_app() as (client, _app):
        calendar_id = await _calendar(client)
        created = await client.post(
            "/calendar/events",
            json={
                "calendarId": calendar_id,
                "title": "Dentist",
                "start": "2026-06-09T10:00:00Z",
                "end": "2026-06-09T11:00:00Z",
                "location": "Clínica Pérez",
            },
        )
        assert created.status_code == 201, created.text
        event = json_body(created)
        event_id = s(event["id"])
        assert event["location"] == "Clínica Pérez"
        assert event["allDay"] is False

        fetched = await client.get(f"/calendar/events/{event_id}")
        assert json_body(fetched)["title"] == "Dentist"

        patched = await client.patch(
            f"/calendar/events/{event_id}", json={"title": "Dentist (moved)"}
        )
        assert json_body(patched)["title"] == "Dentist (moved)"
        assert json_body(patched)["start"] == event["start"]

        assert (await client.delete(f"/calendar/events/{event_id}")).status_code == 204
        assert (await client.get(f"/calendar/events/{event_id}")).status_code == 404


async def test_occurrences_expand_a_series_and_events_do_not():
    async with client_app() as (client, _app):
        calendar_id = await _calendar(client, "Work")
        _ = await client.post(
            "/calendar/events",
            json={
                "calendarId": calendar_id,
                "title": "Standup",
                "start": "2026-06-01T09:00:00Z",
                "end": "2026-06-01T09:15:00Z",
                "rrule": "FREQ=DAILY;COUNT=5",
            },
        )

        stored = await client.get("/calendar/events")
        assert len(arr(json_body(stored)["items"])) == 1

        expanded = await client.get(
            "/calendar/occurrences",
            params={"start": "2026-06-01T00:00:00Z", "end": "2026-06-10T00:00:00Z"},
        )
        items = [obj(item) for item in arr(json_body(expanded)["items"])]
        assert len(items) == 5
        assert all(item["recurring"] for item in items)
        assert s(items[0]["occurrenceId"]).startswith(s(items[0]["eventId"]))


async def test_cancelling_one_occurrence_leaves_the_series():
    async with client_app() as (client, _app):
        calendar_id = await _calendar(client, "Work")
        event = json_body(
            await client.post(
                "/calendar/events",
                json={
                    "calendarId": calendar_id,
                    "title": "Standup",
                    "start": "2026-06-01T09:00:00Z",
                    "rrule": "FREQ=DAILY;COUNT=3",
                },
            )
        )

        cancelled = await client.delete(
            f"/calendar/events/{s(event['id'])}/occurrences",
            params={"start": "2026-06-02T09:00:00Z"},
        )
        assert cancelled.status_code == 200
        assert json_body(cancelled)["rrule"] == "FREQ=DAILY;COUNT=3"

        expanded = await client.get(
            "/calendar/occurrences",
            params={"start": "2026-06-01T00:00:00Z", "end": "2026-06-10T00:00:00Z"},
        )
        assert len(arr(json_body(expanded)["items"])) == 2


async def test_an_over_wide_window_is_refused_rather_than_truncated():
    async with client_app() as (client, _app):
        resp = await client.get(
            "/calendar/occurrences",
            params={"start": "2020-01-01T00:00:00Z", "end": "2040-01-01T00:00:00Z"},
        )
        assert resp.status_code == 422


async def test_an_inverted_window_is_refused():
    async with client_app() as (client, _app):
        resp = await client.get(
            "/calendar/occurrences",
            params={"start": "2026-06-10T00:00:00Z", "end": "2026-06-01T00:00:00Z"},
        )
        assert resp.status_code == 422


async def test_an_unparseable_rule_is_a_422():
    async with client_app() as (client, _app):
        calendar_id = await _calendar(client)
        resp = await client.post(
            "/calendar/events",
            json={
                "calendarId": calendar_id,
                "title": "Nonsense",
                "start": "2026-06-09T10:00:00Z",
                "rrule": "FREQ=NEVER",
            },
        )
        assert resp.status_code == 422


async def test_an_unknown_zone_is_a_422():
    async with client_app() as (client, _app):
        calendar_id = await _calendar(client)
        resp = await client.post(
            "/calendar/events",
            json={
                "calendarId": calendar_id,
                "title": "Elsewhere",
                "start": "2026-06-09T10:00:00Z",
                "timezone": "Mars/Olympus_Mons",
            },
        )
        assert resp.status_code == 422


# --- ICS files -------------------------------------------------------------


async def test_export_then_import_round_trips_through_the_surface():
    async with client_app() as (client, _app):
        source = await _calendar(client, "Work")
        _ = await client.post(
            "/calendar/events",
            json={
                "calendarId": source,
                "title": "Sprint planning",
                "start": "2026-06-08T14:00:00Z",
                "end": "2026-06-08T15:30:00Z",
                "timezone": "Europe/Madrid",
                "rrule": "FREQ=WEEKLY;BYDAY=MO",
            },
        )

        exported = await client.get(f"/calendar/calendars/{source}/export")
        assert exported.status_code == 200
        assert exported.headers["content-type"].startswith("text/calendar")
        assert "BEGIN:VCALENDAR" in exported.text

        target = await _calendar(client, "Imported")
        imported = await client.post(
            f"/calendar/calendars/{target}/import",
            files={"file": ("cal.ics", exported.content, "text/calendar")},
        )
        assert imported.json() == {"created": 1, "updated": 0, "skipped": 0}

        events = json_body(await client.get("/calendar/events", params={"calendarId": target}))
        first = obj(arr(events["items"])[0])
        assert first["timezone"] == "Europe/Madrid"
        assert first["rrule"] == "FREQ=WEEKLY;BYDAY=MO"


async def test_importing_into_an_unknown_calendar_is_a_404():
    async with client_app() as (client, _app):
        resp = await client.post(
            "/calendar/calendars/nope/import",
            files={"file": ("cal.ics", b"BEGIN:VCALENDAR\nEND:VCALENDAR\n", "text/calendar")},
        )
        assert resp.status_code == 404


# --- natural language ------------------------------------------------------


async def test_parsing_a_phrase_returns_a_draft_and_stores_nothing(
    monkeypatch: pytest.MonkeyPatch,
):
    from services.registry import ModelRegistry

    async def respond(_messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        tool = info.output_tools[0].name
        return ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name=tool,
                    args={"title": "Lunch with Ana", "starts_at": "2026-06-12T13:00:00"},
                )
            ]
        )

    async def resolve_detailed(self: ModelRegistry, _role: str, **_kwargs: object):
        return await stub_resolution(self, FunctionModel(respond))

    register_stub_provider(monkeypatch)
    monkeypatch.setattr(ModelRegistry, "resolve_detailed", resolve_detailed)

    async with client_app() as (client, _app):
        resp = await client.post(
            "/calendar/parse",
            json={"phrase": "lunch with Ana Friday 1pm", "timezone": "Europe/Madrid"},
        )
        assert resp.status_code == 200, resp.text
        draft = json_body(resp)
        assert draft["title"] == "Lunch with Ana"
        assert draft["start"] == "2026-06-12T11:00:00Z"  # 13:00 Madrid in June
        # A draft is not an event.
        assert json_body(await client.get("/calendar/events"))["items"] == []


async def test_an_empty_phrase_is_a_422():
    async with client_app() as (client, _app):
        resp = await client.post("/calendar/parse", json={"phrase": "  "})
        assert resp.status_code == 422


async def test_no_utility_model_degrades_to_503(monkeypatch: pytest.MonkeyPatch):
    from core.exceptions import DegradedCapabilityError
    from services.registry import ModelRegistry

    async def resolve_detailed(_self: ModelRegistry, _role: str, **_kwargs: object):
        raise DegradedCapabilityError("no model bound")

    register_stub_provider(monkeypatch)
    monkeypatch.setattr(ModelRegistry, "resolve_detailed", resolve_detailed)

    async with client_app() as (client, _app):
        resp = await client.post("/calendar/parse", json={"phrase": "lunch Friday"})
        assert resp.status_code == 503


# --- the agent's tools ------------------------------------------------------


def _tool(name: str):
    """One tool from the calendar toolset, by name — the same direct-invocation shape
    `tests/test_code_tools.py` uses."""
    return calendar_toolset().tools[name]


def _deps(owner: str, caps: ServiceContainer | None = None) -> RunDeps:
    run = Run(id="t", kind="chat", owner_id=owner, stream=RunStream())
    return RunDeps(run=run, owner_id=owner, caps=caps if caps is not None else ServiceContainer())


def _ctx(deps: RunDeps) -> RunContext[RunDeps]:
    return RunContext(deps=deps, model=TestModel(), usage=RunUsage())


async def _call(deps: RunDeps, name: str, **kwargs: str) -> JsonValue:
    return JSON.validate_python(await _tool(name).function(_ctx(deps), **kwargs))


async def _service_deps(app: FastAPI) -> tuple[str, CalendarService]:
    from routes.deps import OPERATOR_ID

    service = CalendarService(app_state(app, "db_engine", Engine), app_state(app, "vault", Vault))
    return OPERATOR_ID, service


async def test_the_agent_can_read_and_write_the_calendar():
    async with client_app() as (_client, app):
        owner, service = await _service_deps(app)
        calendar = await service.create_calendar(owner, "Personal")
        deps = _deps(owner, ServiceContainer.of(service))

        created = obj(
            await _call(deps, "create_event", title="Dentist", start="2026-06-09T10:00:00Z")
        )
        assert created["calendar_id"] == calendar.id

        found = arr(
            await _call(deps, "agenda", start="2026-06-01T00:00:00Z", end="2026-06-30T00:00:00Z")
        )
        assert [obj(item)["title"] for item in found] == ["Dentist"]

        assert "Deleted" in s(await _call(deps, "delete_event", event_id=s(created["event_id"])))
        assert await service.list_events(owner) == []


async def test_the_agent_falls_back_to_the_first_writable_calendar():
    async with client_app() as (_client, app):
        owner, service = await _service_deps(app)
        _ = await service.create_calendar(owner, "Read-only", read_only=True)
        writable = await service.create_calendar(owner, "Personal")
        deps = _deps(owner, ServiceContainer.of(service))

        created = obj(
            await _call(deps, "create_event", title="Coffee", start="2026-06-09T10:00:00Z")
        )
        assert created["calendar_id"] == writable.id


async def test_a_bad_timestamp_asks_the_model_to_retry():
    from pydantic_ai import ModelRetry

    async with client_app() as (_client, app):
        owner, service = await _service_deps(app)
        _ = await service.create_calendar(owner, "Personal")
        deps = _deps(owner, ServiceContainer.of(service))

        with pytest.raises(ModelRetry):
            _ = await _call(deps, "create_event", title="Whenever", start="next tuesday-ish")


async def test_the_calendar_tools_degrade_when_the_capability_is_absent():
    deps = _deps("operator")
    found = await _call(deps, "agenda")
    assert found == [{"error": "The calendar is unavailable."}]


async def test_the_calendar_tools_are_not_approval_gated():
    """Managing the operator's own schedule is ordinary work, not an `AE-3.1` sensitive
    action — no calendar tool may ask for approval."""
    tools = calendar_toolset().tools
    assert set(tools) == {
        "agenda",
        "list_calendars",
        "create_event",
        "update_event",
        "delete_event",
    }
    assert not any(tool.requires_approval for tool in tools.values())


async def test_editing_a_series_can_drop_its_repeat():
    """`CAL-1`'s edit over a recurring event. Turning a series back into a single event
    needs `clearRrule`, because a partial patch reads an omitted `rrule` as "leave it
    alone" — so there would otherwise be no way to express it, and the calendar screen's
    "No recurrence" choice would silently do nothing."""
    async with client_app() as (client, _app):
        calendar_id = await _calendar(client)
        created = await client.post(
            "/calendar/events",
            json={
                "calendarId": calendar_id,
                "title": "Standup",
                "start": "2026-06-08T09:00:00Z",
                "end": "2026-06-08T09:15:00Z",
                "rrule": "FREQ=DAILY",
            },
        )
        event = json_body(created)
        event_id = s(event["id"])
        assert event["rrule"] == "FREQ=DAILY"

        # A plain edit leaves the rule intact — this is the case `clearRrule` has to be
        # distinguishable from.
        kept = await client.patch(f"/calendar/events/{event_id}", json={"title": "Standup (async)"})
        assert json_body(kept)["rrule"] == "FREQ=DAILY"

        dropped = await client.patch(f"/calendar/events/{event_id}", json={"clearRrule": True})
        assert json_body(dropped)["rrule"] is None

        # And the window now yields one event rather than a run of occurrences.
        window = await client.get(
            "/calendar/occurrences?start=2026-06-01T00:00:00Z&end=2026-06-30T00:00:00Z"
        )
        assert len(arr(json_body(window)["items"])) == 1


async def test_editing_an_event_moves_it_and_keeps_its_zone():
    """The screen sends a wall-clock time plus the browser's zone, so a moved event has
    to land at the instant the operator picked — not shifted by their offset."""
    async with client_app() as (client, _app):
        calendar_id = await _calendar(client)
        created = await client.post(
            "/calendar/events",
            json={
                "calendarId": calendar_id,
                "title": "Review",
                "start": "2026-06-09T10:00:00Z",
                "end": "2026-06-09T11:00:00Z",
                "timezone": "Europe/Madrid",
            },
        )
        event = json_body(created)

        moved = await client.patch(
            f"/calendar/events/{s(event['id'])}",
            json={
                "start": "2026-06-10T14:00:00Z",
                "end": "2026-06-10T15:00:00Z",
                "timezone": "Europe/Madrid",
                "location": "Room 2",
            },
        )
        assert moved.status_code == 200, moved.text
        body = json_body(moved)
        assert s(body["start"]).startswith("2026-06-10T14:00")
        assert body["location"] == "Room 2"
        assert body["timezone"] == "Europe/Madrid"
        assert body["title"] == "Review"  # an omitted field is left alone
