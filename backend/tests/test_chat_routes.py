"""POST /chat creates a Run that streams over the substrate SSE surface."""

from __future__ import annotations

from fastapi import HTTPException

from core.config import get_settings

from ._helpers import client_app, collect_sse_events, patch_model_resolution


async def test_chat_creates_run_and_streams_answer(monkeypatch):
    # The route resolves the `main` role through the registry; point that at a
    # TestModel so the turn runs without a live model server.
    patch_model_resolution(monkeypatch)

    async with client_app() as (client, _app):
        resp = await client.post("/chat", json={"prompt": "say hi"})
        assert resp.status_code == 202
        run_id = resp.json()["run_id"]

        events = await collect_sse_events(client, run_id)

    types = [e["type"] for e in events]
    assert types[0] == "run.started"
    assert types[-1] == "run.ended"
    answer = "".join(e["text"] for e in events if e["type"] == "answer.delta")
    assert answer == "hi"


async def test_ephemeral_chat_is_not_titled(monkeypatch):
    # Ephemeral (compare) threads are hidden from the listing and show no title, so
    # auto-titling them is invisible work that only holds the run open after the
    # answer — the route disables it. A normal first turn is still named.
    patch_model_resolution(monkeypatch)

    async with client_app() as (client, _app):
        eph = await client.post("/chat", json={"prompt": "hi", "ephemeral": True})
        eph_events = await collect_sse_events(client, eph.json()["run_id"])

        normal = await client.post("/chat", json={"prompt": "hi"})
        normal_events = await collect_sse_events(client, normal.json()["run_id"])

    assert not any(e["type"] == "conversation.titled" for e in eph_events)
    assert any(e["type"] == "conversation.titled" for e in normal_events)


async def test_chat_requires_prompt():
    async with client_app() as (client, _app):
        resp = await client.post("/chat", json={})
        assert resp.status_code == 422


async def test_chat_rejects_unknown_conversation(monkeypatch):
    # A client-supplied conversation_id that doesn't exist must 404, not silently
    # spawn orphan messages under a phantom conversation.
    patch_model_resolution(monkeypatch)

    async with client_app() as (client, _app):
        resp = await client.post(
            "/chat", json={"prompt": "hello", "conversation_id": "does-not-exist"}
        )
        assert resp.status_code == 404


async def test_chat_settings_round_trip():
    # The auto-compaction preferences are operator settings: GET reports the config
    # defaults until set, PUT overrides them, and the override persists.
    async with client_app() as (client, _app):
        got = await client.get("/chat/settings")
        assert got.status_code == 200
        assert got.json()["auto_compact_threshold"] == get_settings().auto_compact_threshold

        put = await client.put("/chat/settings", json={"auto_compact_threshold": 0.5})
        assert put.status_code == 200
        assert put.json()["auto_compact_threshold"] == 0.5

        again = await client.get("/chat/settings")
        assert again.json()["auto_compact_threshold"] == 0.5


async def test_chat_settings_rejects_an_out_of_range_threshold():
    # A fraction in (0, 1]: 0 would fire on an empty thread, above 1 could never fire.
    async with client_app() as (client, _app):
        for bad in (0, -0.5, 1.5):
            resp = await client.put("/chat/settings", json={"auto_compact_threshold": bad})
            assert resp.status_code == 422


async def test_chat_settings_rejects_a_retired_field():
    # The tool-result compaction settings are gone. A client still sending them must get a
    # 422, not a 200 that silently discards the write.
    async with client_app() as (client, _app):
        retired = (
            "compaction_enabled",
            "compaction_keep_recent",
            "compaction_min_tokens",
            "attachment_inline_max_tokens",
        )
        for field in retired:
            resp = await client.put("/chat/settings", json={field: 5})
            assert resp.status_code == 422


async def test_chat_settings_rejects_an_unknown_field():
    # With every field optional (omitted ⇒ unchanged), a mistyped key must 422 rather than
    # silently no-op with a 200 — otherwise a client believes a write landed that never did.
    async with client_app() as (client, _app):
        resp = await client.put(
            "/chat/settings",
            json={"agent_request_limi": 15},  # typo: missing 't'
        )
        assert resp.status_code == 422


async def test_agent_request_limit_round_trip():
    # The per-turn model-request ceiling is an operator setting: GET reports the config
    # default until set, PUT overrides it, and the override persists.
    async with client_app() as (client, _app):
        got = (await client.get("/chat/settings")).json()
        assert got["agent_request_limit"] == get_settings().agent_request_limit

        put = await client.put("/chat/settings", json={"agent_request_limit": 60})
        assert put.status_code == 200
        assert put.json()["agent_request_limit"] == 60

        again = (await client.get("/chat/settings")).json()
        assert again["agent_request_limit"] == 60


async def test_agent_request_limit_rejects_zero_and_negative():
    # Floored at 1, unlike the token caps: a turn allowed zero model requests could
    # never produce an answer, so 0 is nonsensical rather than merely minimal.
    async with client_app() as (client, _app):
        for bad in (0, -1):
            resp = await client.put("/chat/settings", json={"agent_request_limit": bad})
            assert resp.status_code == 422


async def test_agent_request_limit_partial_update_leaves_others_unchanged():
    # Tuning the step limit alone must not reset the auto-compaction overrides.
    async with client_app() as (client, _app):
        await client.put(
            "/chat/settings",
            json={"auto_compact_enabled": False, "auto_compact_threshold": 0.6},
        )
        await client.put("/chat/settings", json={"agent_request_limit": 40})
        again = (await client.get("/chat/settings")).json()
        assert again["agent_request_limit"] == 40
        assert again["auto_compact_enabled"] is False
        assert again["auto_compact_threshold"] == 0.6


async def test_auto_compact_settings_partial_update_leaves_others_unchanged():
    # Tuning one half of the pair must not reset the other, set earlier.
    async with client_app() as (client, _app):
        await client.put(
            "/chat/settings",
            json={"auto_compact_enabled": False, "auto_compact_threshold": 0.6},
        )
        await client.put("/chat/settings", json={"auto_compact_threshold": 0.8})
        again = (await client.get("/chat/settings")).json()
        assert again["auto_compact_enabled"] is False  # preserved across the threshold-only PUT
        assert again["auto_compact_threshold"] == 0.8


async def test_inactivity_timeout_round_trip():
    # The inactivity bound is an operator setting: GET reports the config default until
    # set, PUT overrides it, and the override persists.
    async with client_app() as (client, _app):
        got = (await client.get("/chat/settings")).json()
        assert got["inactivity_timeout_s"] == get_settings().run_inactivity_timeout_s

        put = await client.put("/chat/settings", json={"inactivity_timeout_s": 300})
        assert put.status_code == 200
        assert put.json()["inactivity_timeout_s"] == 300

        again = (await client.get("/chat/settings")).json()
        assert again["inactivity_timeout_s"] == 300


async def test_wall_clock_timeout_is_absent_until_the_operator_sets_one():
    # The default is no bound at all: a turn is already capped by `agent_request_limit`, so
    # a clock only ever stops a run that is legitimately slow.
    async with client_app() as (client, _app):
        got = (await client.get("/chat/settings")).json()
        assert got["wall_clock_timeout_s"] is None

        put = await client.put("/chat/settings", json={"wall_clock_timeout_s": 3600})
        assert put.status_code == 200
        assert put.json()["wall_clock_timeout_s"] == 3600

        again = (await client.get("/chat/settings")).json()
        assert again["wall_clock_timeout_s"] == 3600


async def test_explicit_null_removes_the_wall_clock_bound():
    # `null` is the one body value that means something rather than "unchanged", so a PUT
    # carrying it must clear the bound — and a PUT that omits the field must not.
    async with client_app() as (client, _app):
        await client.put("/chat/settings", json={"wall_clock_timeout_s": 3600})

        untouched = await client.put("/chat/settings", json={"agent_request_limit": 30})
        assert untouched.json()["wall_clock_timeout_s"] == 3600

        cleared = await client.put("/chat/settings", json={"wall_clock_timeout_s": None})
        assert cleared.status_code == 200
        assert cleared.json()["wall_clock_timeout_s"] is None
        assert (await client.get("/chat/settings")).json()["wall_clock_timeout_s"] is None


async def test_the_subagent_cap_is_absent_until_the_operator_sets_one():
    # Unlimited is the default and not a fallback from a config value: the model launches
    # sub-agents to save the operator time, and a number chosen before anyone knows what
    # the work splits into would mostly be the wrong one.
    async with client_app() as (client, _app):
        assert (await client.get("/chat/settings")).json()["subagent_max_concurrent"] is None

        put = await client.put("/chat/settings", json={"subagent_max_concurrent": 2})
        assert put.status_code == 200
        assert put.json()["subagent_max_concurrent"] == 2
        assert (await client.get("/chat/settings")).json()["subagent_max_concurrent"] == 2


async def test_explicit_null_removes_the_subagent_cap():
    # The second field where `null` is a value rather than an omission.
    async with client_app() as (client, _app):
        await client.put("/chat/settings", json={"subagent_max_concurrent": 2})

        untouched = await client.put("/chat/settings", json={"agent_request_limit": 30})
        assert untouched.json()["subagent_max_concurrent"] == 2

        cleared = await client.put("/chat/settings", json={"subagent_max_concurrent": None})
        assert cleared.json()["subagent_max_concurrent"] is None
        assert (await client.get("/chat/settings")).json()["subagent_max_concurrent"] is None


async def test_the_subagent_cap_rejects_zero_and_negative():
    # "None at all" is what switching the tool off in the catalog says. A cap of 0 would be
    # a second, obscurer way of saying it, reachable by typing in a number box.
    async with client_app() as (client, _app):
        for bad in (0, -1):
            resp = await client.put("/chat/settings", json={"subagent_max_concurrent": bad})
            assert resp.status_code == 422


async def test_wall_clock_timeout_rejects_zero_and_negative():
    # Off is `null`, not 0 — a 0 bound would stop every turn the instant it started.
    async with client_app() as (client, _app):
        for bad in (0, -1):
            resp = await client.put("/chat/settings", json={"wall_clock_timeout_s": bad})
            assert resp.status_code == 422


async def test_inactivity_timeout_rejects_zero_and_negative():
    # A 0 bound would stop every turn immediately, and a negative one is nonsensical —
    # both are rejected rather than silently disabling the watchdog.
    async with client_app() as (client, _app):
        for bad in (0, -1):
            resp = await client.put("/chat/settings", json={"inactivity_timeout_s": bad})
            assert resp.status_code == 422


async def test_continue_turn_retires_the_stop_marker(monkeypatch):
    # The "Continue" button under a stopped turn resumes it as an ordinary turn and
    # names the turn it resumes. Accepting that turn must retire the marker durably —
    # a warning that survives the operator acting on it is a warning they learn to
    # ignore.
    patch_model_resolution(monkeypatch)

    async with client_app() as (client, app):
        first = await client.post("/chat", json={"prompt": "say hi"})
        conv = first.json()["conversation_id"]
        await collect_sse_events(client, first.json()["run_id"])

        store = app.state.conversations
        stopped = (await store.messages_view(conv))[-1]
        # Stamp a marker the way a bound-stopped run would, then continue that turn.
        store._cache_get(conv).nodes[stopped.id].blocked_reason = "cancelled by the operator"
        assert (await store.messages_view(conv))[-1].blocked_reason is not None

        resumed = await client.post(
            "/chat",
            json={
                "prompt": "Continue.",
                "conversation_id": conv,
                "continues_message_id": stopped.id,
            },
        )
        assert resumed.status_code == 202
        await collect_sse_events(client, resumed.json()["run_id"])

        detail = await client.get(f"/conversations/{conv}")
        assert all(m["blocked_reason"] is None for m in detail.json()["messages"])


async def test_a_rejected_send_leaves_the_stop_marker_alone(monkeypatch):
    # A turn the backend refuses outright leaves the operator with the same
    # unfinished turn, so it has to leave them the prompt to resume it too.
    patch_model_resolution(monkeypatch)

    async with client_app() as (client, app):
        first = await client.post("/chat", json={"prompt": "say hi"})
        conv = first.json()["conversation_id"]
        await collect_sse_events(client, first.json()["run_id"])

        store = app.state.conversations
        stopped = (await store.messages_view(conv))[-1]
        store._cache_get(conv).nodes[stopped.id].blocked_reason = "cancelled by the operator"

        rejected = await client.post(
            "/chat",
            json={"prompt": "", "conversation_id": conv, "continues_message_id": stopped.id},
        )
        assert rejected.status_code == 422
        assert (await store.messages_view(conv))[-1].blocked_reason is not None


async def test_a_turn_that_fails_to_submit_leaves_the_stop_marker_alone(monkeypatch):
    # The route's own claim is not proof the turn will run: `submit` has its own atomic
    # check-and-claim, and a race it catches surfaces as a 409 from inside `_submit_turn`.
    # Retiring the marker before that point would strand the operator with an unfinished
    # turn and no button left to resume it.
    patch_model_resolution(monkeypatch)

    async with client_app() as (client, app):
        first = await client.post("/chat", json={"prompt": "say hi"})
        conv = first.json()["conversation_id"]
        await collect_sse_events(client, first.json()["run_id"])

        store = app.state.conversations
        stopped = (await store.messages_view(conv))[-1]
        store._cache_get(conv).nodes[stopped.id].blocked_reason = "cancelled by the operator"

        import routes.chat.submit as chat_routes

        def busy(**_kwargs):
            raise HTTPException(status_code=409, detail="lost the race")

        monkeypatch.setattr(chat_routes, "compose_turn", busy)

        lost = await client.post(
            "/chat",
            json={
                "prompt": "Continue.",
                "conversation_id": conv,
                "continues_message_id": stopped.id,
            },
        )
        assert lost.status_code == 409
        assert (await store.messages_view(conv))[-1].blocked_reason is not None


async def test_chat_with_an_unknown_agent_is_refused(monkeypatch):
    patch_model_resolution(monkeypatch)

    async with client_app() as (client, _app):
        resp = await client.post("/chat", json={"prompt": "hi", "agent": "nobody"})
        assert resp.status_code == 422


async def test_chat_with_an_unpublished_agent_is_refused(monkeypatch):
    patch_model_resolution(monkeypatch)

    async with client_app() as (client, _app):
        await client.post(
            "/agents", json={"name": "wip", "description": "Not yet.", "instructions": "x"}
        )
        resp = await client.post("/chat", json={"prompt": "hi", "agent": "wip"})
        assert resp.status_code == 422


async def test_a_new_thread_stores_the_agent_it_runs(monkeypatch):
    patch_model_resolution(monkeypatch)

    async with client_app() as (client, app):
        made = await client.post(
            "/agents", json={"name": "scout", "description": "Looks.", "instructions": "x"}
        )
        await client.post(f"/agents/{made.json()['id']}/publish")

        resp = await client.post("/chat", json={"prompt": "hi", "agent": "scout"})
        assert resp.status_code == 202
        await collect_sse_events(client, resp.json()["run_id"])

        listed = (await client.get("/conversations")).json()
        assert [c["agent_name"] for c in listed] == ["scout"]
        conversation_id = listed[0]["id"]

        again = await client.post(
            "/chat",
            json={"prompt": "again", "conversation_id": conversation_id, "agent": "default"},
        )
        assert again.status_code == 202
        await collect_sse_events(client, again.json()["run_id"])
        assert (await app.state.conversations.binding(conversation_id)).agent_name is None
