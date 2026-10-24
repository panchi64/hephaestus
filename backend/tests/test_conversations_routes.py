"""The conversations REST surface — list, read (projected history), rename, delete.

A conversation comes into being as a side effect of a chat turn, so each test
drives ``POST /chat`` (against a TestModel) to create one, then exercises the
read/manage endpoints over it.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import JsonValue

from routes.conversations import message_versions
from routes.deps import app_state
from runs import Run
from services.conversation_view import MessageView, ToolView
from services.registry import ModelRegistry, ResolvedModel
from services.workspace_history import SnapshotView, format_show_result

from ._helpers import client_app, collect_sse_events, patch_model_resolution
from ._json import arr, json_body, json_list, obj, s


async def _start_conversation(client: httpx.AsyncClient, prompt: str = "say hi") -> str:
    """Run one chat turn and return its conversation id, draining the stream so
    the turn's messages are recorded before we read them back."""
    resp = await client.post("/chat", json={"prompt": prompt})
    assert resp.status_code == 202
    body = json_body(resp)
    _ = await collect_sse_events(client, s(body["run_id"]))
    return s(body["conversation_id"])


async def _rows(client: httpx.AsyncClient) -> list[JsonValue]:
    return json_list(await client.get("/conversations"))


async def _first_row(client: httpx.AsyncClient) -> dict[str, JsonValue]:
    return obj((await _rows(client))[0])


async def _detail(client: httpx.AsyncClient, conversation_id: str) -> dict[str, JsonValue]:
    return json_body(await client.get(f"/conversations/{conversation_id}"))


async def test_list_conversations(monkeypatch: pytest.MonkeyPatch):
    patch_model_resolution(monkeypatch, output_text="hello there")
    async with client_app() as (client, _app):
        conversation_id = await _start_conversation(client)

        resp = await client.get("/conversations")
        assert resp.status_code == 200
        rows = json_list(resp)
        assert len(rows) == 1
        row = obj(rows[0])
        assert row["id"] == conversation_id
        assert row["message_count"] == 2  # user prompt + assistant answer
        assert row["preview"]  # derived from the latest message text
        assert row["model"] == "test"  # the model the turn ran on (TestModel)


async def test_summary_model_survives_a_cold_read(monkeypatch: pytest.MonkeyPatch):
    """The last-used model is durable: it's recovered from the stored response blob
    after the in-memory tree is evicted, not only while the conversation is warm."""
    patch_model_resolution(monkeypatch, output_text="hello there")
    async with client_app() as (client, app):
        conversation_id = await _start_conversation(client)
        store = app_state(app).conversations
        store.evict_cache()  # force the cold (DB) summary path

        await store.flush()  # let the write-behind drainer settle the model
        store.evict_cache()  # force the cold (DB) summary path

        rows = await _rows(client)
        assert obj(rows[0])["id"] == conversation_id
        assert obj(rows[0])["model"] == "test"


async def test_get_conversation_projects_history(monkeypatch: pytest.MonkeyPatch):
    patch_model_resolution(monkeypatch, output_text="hello there")
    async with client_app() as (client, _app):
        conversation_id = await _start_conversation(client, prompt="say hi")

        resp = await client.get(f"/conversations/{conversation_id}")
        assert resp.status_code == 200
        detail = json_body(resp)
        assert detail["id"] == conversation_id
        messages = [obj(m) for m in arr(detail["messages"])]
        assert [m["role"] for m in messages] == ["user", "assistant"]
        assert messages[0]["content"] == "say hi"
        assert messages[0]["model"] is None  # user turns carry no model
        assert messages[1]["content"] == "hello there"
        assert messages[1]["tools"] == []
        assert messages[1]["model"] == "test"  # the assistant turn's model


async def test_get_unknown_conversation_404():
    async with client_app() as (client, _app):
        resp = await client.get("/conversations/does-not-exist")
        assert resp.status_code == 404


async def test_detail_reports_an_in_flight_run(monkeypatch: pytest.MonkeyPatch):
    """A turn still streaming server-side surfaces as ``active_run`` on the cold
    read, so a reattaching client (e.g. a page reload mid-stream) can resume it
    instead of rendering the thread reply-less."""
    patch_model_resolution(monkeypatch, output_text="hello there")
    async with client_app() as (client, app):
        conversation_id = await _start_conversation(client)

        # The completed turn left no live run.
        first = await _detail(client, conversation_id)
        assert first["active_run"] is None

        # Simulate a fresh turn that's still running for this conversation.
        started, release = asyncio.Event(), asyncio.Event()

        async def orch(_run: Run) -> None:
            started.set()
            _ = await release.wait()

        run = app_state(app).runs.submit(
            kind="chat",
            owner_id="operator",
            orchestrator=orch,
            conversation_id=conversation_id,
        )
        _ = await started.wait()

        detail = await _detail(client, conversation_id)
        active = obj(detail["active_run"])
        assert active["id"] == run.id
        # The kind rides along so a reattaching client knows whether to seed an
        # assistant turn — a fold is a run that writes none.
        assert active["kind"] == "chat"
        assert active["status"] == "running"
        assert isinstance(active["last_seq"], int)

        release.set()
        await run.wait()

        # Terminal again → the read no longer advertises a live run.
        after = await _detail(client, conversation_id)
        assert after["active_run"] is None


async def test_list_reports_which_threads_are_working(monkeypatch: pytest.MonkeyPatch):
    """Each row carries the status of the run driving it, so the thread list can mark
    a working conversation without opening it. Registry-derived: a turn isn't
    persisted until it finishes, so the stored summary alone can't tell."""
    patch_model_resolution(monkeypatch, output_text="hello there")
    async with client_app() as (client, app):
        conversation_id = await _start_conversation(client)

        # The completed turn left no live run — the row is at rest.
        assert (await _first_row(client))["activity"] is None

        started, release = asyncio.Event(), asyncio.Event()

        async def orch(_run: Run) -> None:
            started.set()
            _ = await release.wait()

        run = app_state(app).runs.submit(
            kind="chat",
            owner_id="operator",
            orchestrator=orch,
            conversation_id=conversation_id,
        )
        _ = await started.wait()

        assert (await _first_row(client))["activity"] == "running"

        release.set()
        await run.wait()

        # Terminal again → the row goes back to rest.
        assert (await _first_row(client))["activity"] is None


async def test_list_distinguishes_a_parked_run_from_a_streaming_one(
    monkeypatch: pytest.MonkeyPatch,
):
    """A run parked on the operator's approval decision reports its own
    ``awaiting_input`` status rather than folding into plain activity — it's a
    "needs YOU" signal, and the list tones it differently."""
    patch_model_resolution(monkeypatch, output_text="hello there")
    async with client_app() as (client, app):
        conversation_id = await _start_conversation(client)

        async def orch(run: Run) -> None:
            run.park(None)

        runs = app_state(app).runs
        run = runs.submit(
            kind="chat",
            owner_id="operator",
            orchestrator=orch,
            conversation_id=conversation_id,
        )
        await run.wait()

        rows = await _rows(client)
        assert obj(rows[0])["activity"] == "awaiting_input"

        # Parking is non-terminal; decide it out of band so the app tears down clean.
        assert await runs.cancel(run.id) is True
        assert (await _first_row(client))["activity"] is None


async def test_a_finished_turn_is_distinguishable_from_a_thread_that_never_ran(
    monkeypatch: pytest.MonkeyPatch,
):
    """`activity` covers the three live statuses only, so a terminal run used to collapse
    to the same null an idle thread reports — done, failed and never-started were one
    row. `last_outcome` is the sibling that separates them."""
    patch_model_resolution(monkeypatch, output_text="hello there")
    async with client_app() as (client, _app):
        conversation_id = await _start_conversation(client)

        row = await _first_row(client)
        assert row["activity"] is None
        assert row["last_outcome"] == "done"

        # And on the read of the thread itself, which carries the listing row's shape.
        detail = await _detail(client, conversation_id)
        assert detail["last_outcome"] == "done"


async def test_a_failed_run_says_so_on_the_row(monkeypatch: pytest.MonkeyPatch):
    """The whole point: a thread whose last turn blew up looks different in the rail
    from one sitting quietly, without opening it."""
    patch_model_resolution(monkeypatch, output_text="hello there")
    async with client_app() as (client, app):
        conversation_id = await _start_conversation(client)

        async def orch(_run: Run) -> None:
            raise RuntimeError("the turn fell over")

        run = app_state(app).runs.submit(
            kind="chat",
            owner_id="operator",
            orchestrator=orch,
            conversation_id=conversation_id,
        )
        await run.wait()

        row = await _first_row(client)
        assert row["activity"] is None
        assert row["last_outcome"] == "error"


async def test_a_live_run_keeps_the_previous_outcome_beside_it(monkeypatch: pytest.MonkeyPatch):
    """The two fields answer different questions and are read together: what this thread
    is doing now, and how it last ended. A thread can be running having failed before."""
    patch_model_resolution(monkeypatch, output_text="hello there")
    async with client_app() as (client, app):
        conversation_id = await _start_conversation(client)
        started, release = asyncio.Event(), asyncio.Event()

        async def orch(_run: Run) -> None:
            started.set()
            _ = await release.wait()

        run = app_state(app).runs.submit(
            kind="chat",
            owner_id="operator",
            orchestrator=orch,
            conversation_id=conversation_id,
        )
        _ = await started.wait()

        row = await _first_row(client)
        assert row["activity"] == "running"
        assert row["last_outcome"] == "done"  # the turn that created the thread

        release.set()
        await run.wait()


async def test_rename_conversation(monkeypatch: pytest.MonkeyPatch):
    patch_model_resolution(monkeypatch, output_text="hello there")
    async with client_app() as (client, _app):
        conversation_id = await _start_conversation(client)

        resp = await client.patch(
            f"/conversations/{conversation_id}", json={"title": "Migration plan"}
        )
        assert resp.status_code == 200
        assert json_body(resp)["title"] == "Migration plan"

        detail = await client.get(f"/conversations/{conversation_id}")
        assert json_body(detail)["title"] == "Migration plan"


async def test_retitle_overwrites_even_an_operator_title(monkeypatch: pytest.MonkeyPatch):
    # A manual re-title is a deliberate operator action, so unlike the fill-only-if-blank
    # auto-titler it overwrites unconditionally — even a name the operator just set.
    patch_model_resolution(monkeypatch, output_text="Regenerated Name")
    async with client_app() as (client, _app):
        conversation_id = await _start_conversation(client)
        _ = await client.patch(f"/conversations/{conversation_id}", json={"title": "Operator Name"})

        resp = await client.post(f"/conversations/{conversation_id}/retitle")
        assert resp.status_code == 200
        assert json_body(resp)["title"] == "Regenerated Name"

        detail = await client.get(f"/conversations/{conversation_id}")
        assert json_body(detail)["title"] == "Regenerated Name"


async def test_retitle_resolves_background_with_the_picker_override(
    monkeypatch: pytest.MonkeyPatch,
):
    # The conversation persists no endpoint, so a manual re-title names the thread with
    # the operator's current pick (like a chat turn) — not a default role they may
    # never have bound. The picked endpoint/model is forwarded to background resolution
    # (utility → the picked main), which carries reasoning-off so a thinking title model
    # doesn't burn its output budget before emitting a title.
    patch_model_resolution(monkeypatch, output_text="Picked Title")
    async with client_app() as (client, _app):
        conversation_id = await _start_conversation(client)

        seen: dict[str, str | None] = {}
        real_resolve = ModelRegistry.resolve_background

        async def spy(
            self: ModelRegistry,
            *,
            owner_id: str,
            override_endpoint_id: str | None = None,
            override_model: str | None = None,
        ) -> ResolvedModel:
            seen["endpoint_id"] = override_endpoint_id
            seen["model"] = override_model
            return await real_resolve(
                self,
                owner_id=owner_id,
                override_endpoint_id=override_endpoint_id,
                override_model=override_model,
            )

        monkeypatch.setattr(ModelRegistry, "resolve_background", spy)

        resp = await client.post(
            f"/conversations/{conversation_id}/retitle",
            json={"endpoint_id": "ep-1", "model": "qwen3"},
        )
        assert resp.status_code == 200
        assert json_body(resp)["title"] == "Picked Title"
        assert seen == {"endpoint_id": "ep-1", "model": "qwen3"}


async def test_retitle_unknown_conversation_404():
    async with client_app() as (client, _app):
        resp = await client.post("/conversations/nope/retitle")
        assert resp.status_code == 404


async def test_title_wait_returns_the_stored_title(monkeypatch: pytest.MonkeyPatch):
    patch_model_resolution(monkeypatch, output_text="Answer")
    async with client_app() as (client, _app):
        conversation_id = await _start_conversation(client)
        _ = await client.patch(f"/conversations/{conversation_id}", json={"title": "Named"})

        resp = await client.get(f"/conversations/{conversation_id}/title")
        assert resp.status_code == 200
        assert json_body(resp) == {"title": "Named"}


async def test_title_wait_holds_until_the_runs_afterwork_names_the_thread():
    async with client_app() as (client, app):
        state = app_state(app)
        conversation_id = await state.conversations.create_conversation("operator")

        async def late_name() -> None:
            await asyncio.sleep(0.2)
            _ = await state.conversations.set_title_if_absent(conversation_id, "Late Name")

        async def orchestrate(run: Run) -> None:
            run.outlive(asyncio.create_task(late_name()))

        run = state.runs.submit(
            kind="chat",
            owner_id="operator",
            orchestrator=orchestrate,
            conversation_id=conversation_id,
        )
        _ = await run.wait()

        resp = await client.get(f"/conversations/{conversation_id}/title")
        assert resp.status_code == 200
        assert json_body(resp) == {"title": "Late Name"}


async def test_title_wait_unknown_conversation_404():
    async with client_app() as (client, _app):
        resp = await client.get("/conversations/nope/title")
        assert resp.status_code == 404


async def test_rename_unknown_conversation_404():
    async with client_app() as (client, _app):
        resp = await client.patch("/conversations/nope", json={"title": "x"})
        assert resp.status_code == 404


async def test_delete_conversation(monkeypatch: pytest.MonkeyPatch):
    patch_model_resolution(monkeypatch, output_text="hello there")
    async with client_app() as (client, _app):
        conversation_id = await _start_conversation(client)

        resp = await client.delete(f"/conversations/{conversation_id}")
        assert resp.status_code == 204

        assert (await client.get(f"/conversations/{conversation_id}")).status_code == 404
        assert (await client.get("/conversations")).json() == []


async def test_delete_unknown_conversation_404():
    async with client_app() as (client, _app):
        resp = await client.delete("/conversations/nope")
        assert resp.status_code == 404


class _RecordingPurger:
    """A stand-in for a manager that owns per-conversation state (the sandbox, the
    browser): records purge calls, and optionally fails them."""

    def __init__(self, *, fail: bool = False) -> None:
        self.purged: list[str] = []
        self._fail: bool = fail

    async def purge(self, key: str) -> None:
        self.purged.append(key)
        if self._fail:
            raise RuntimeError("teardown blew up")


async def test_delete_conversation_purges_its_sandbox(monkeypatch: pytest.MonkeyPatch):
    patch_model_resolution(monkeypatch, output_text="hi")
    async with client_app() as (client, app):
        conversation_id = await _start_conversation(client)
        sandbox = _RecordingPurger()
        app_state(app).conversation_teardown.sandbox = sandbox

        resp = await client.delete(f"/conversations/{conversation_id}")
        assert resp.status_code == 204
        assert sandbox.purged == [conversation_id]


async def test_delete_conversation_purges_its_browser(monkeypatch: pytest.MonkeyPatch):
    # The saved login is the point: it holds the cookies for every site the thread signed
    # into, and it would otherwise outlive the thread the operator deliberately destroyed.
    patch_model_resolution(monkeypatch, output_text="hi")
    async with client_app() as (client, app):
        conversation_id = await _start_conversation(client)
        browser = _RecordingPurger()
        app_state(app).conversation_teardown.browser = browser

        resp = await client.delete(f"/conversations/{conversation_id}")
        assert resp.status_code == 204
        assert browser.purged == [conversation_id]


async def test_delete_conversation_survives_a_failing_purge(monkeypatch: pytest.MonkeyPatch):
    patch_model_resolution(monkeypatch, output_text="hi")
    async with client_app() as (client, app):
        conversation_id = await _start_conversation(client)
        teardown = app_state(app).conversation_teardown
        teardown.sandbox = _RecordingPurger(fail=True)
        browser = _RecordingPurger(fail=True)
        teardown.browser = browser

        # The DB delete is authoritative; a best-effort purge failure must not fail it.
        resp = await client.delete(f"/conversations/{conversation_id}")
        assert resp.status_code == 204
        assert (await client.get(f"/conversations/{conversation_id}")).status_code == 404
        assert browser.purged == [conversation_id]


def _snapshot_view(snapshot_id: str) -> SnapshotView:
    return SnapshotView(
        id=snapshot_id,
        conversation_id="conv-1",
        title="Chart",
        created_at=datetime.now(UTC),
        files_changed=1,
        summary="+1 ~0 -0",
        stats={"added": 1, "modified": 0, "removed": 0},
        preview_artifact_id="a1",
        preview_kind="image",
        keeper=False,
    )


def test_cold_read_reattaches_view_version():
    # A `show` on a turn re-attaches its version (an inline chip) when the conversation
    # is read cold: the tool result embeds the snapshot id, which keys the by-id map.
    snap = _snapshot_view("a1b2c3")
    tool = ToolView(
        id="t1",
        name="view_show",
        args={},
        status="ok",
        result=format_show_result(snap, "image"),
    )
    message = MessageView(role="assistant", tools=[tool])
    refs = message_versions(message, {snap.id: snap})
    assert [r.snapshot_id for r in refs] == ["a1b2c3"]
    assert refs[0].preview_kind == "image"
    assert refs[0].title == "Chart"


def test_cold_read_skips_view_without_version_id():
    # A degraded capture (no id embedded) attaches nothing, even though the tool ran.
    tool = ToolView(
        id="t1",
        name="view_show",
        args={},
        status="ok",
        result="Could not read 'x.html': no such file",
    )
    message = MessageView(role="assistant", tools=[tool])
    assert message_versions(message, {}) == []


async def test_auto_compact_override_round_trip(monkeypatch: pytest.MonkeyPatch):
    patch_model_resolution(monkeypatch)
    async with client_app() as (client, _app):
        cid = await _start_conversation(client)

        # Defaults to null — the thread inherits the operator's global setting.
        got = await client.get(f"/conversations/{cid}/auto-compact")
        assert got.status_code == 200 and got.json()["override"] is None

        # Force compaction off for this thread; it persists.
        put = await client.put(f"/conversations/{cid}/auto-compact", json={"override": False})
        assert put.status_code == 200 and put.json()["override"] is False
        back = await client.get(f"/conversations/{cid}/auto-compact")
        assert back.json()["override"] is False

        # Clear it back to inherit.
        _ = await client.put(f"/conversations/{cid}/auto-compact", json={"override": None})
        assert (await client.get(f"/conversations/{cid}/auto-compact")).json()["override"] is None


async def test_auto_compact_override_unknown_conversation_404(monkeypatch: pytest.MonkeyPatch):
    patch_model_resolution(monkeypatch)
    async with client_app() as (client, _app):
        resp = await client.get("/conversations/does-not-exist/auto-compact")
        assert resp.status_code == 404


async def test_retired_tool_result_compaction_route_is_gone(monkeypatch: pytest.MonkeyPatch):
    # Tool-result compaction was removed; its per-thread override went with it. The
    # surviving `/auto-compact` route must not have quietly inherited the old path.
    patch_model_resolution(monkeypatch)
    async with client_app() as (client, _app):
        cid = await _start_conversation(client)
        assert (await client.get(f"/conversations/{cid}/compaction")).status_code == 404
