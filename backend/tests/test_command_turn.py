"""A turn sent with a slash command: what the model gets, and what is written down.

The whole design rests on those two being different. The operator's literal `/reviewer
check the auth path` is their turn and persists; the expansion rides the tail of the same
user prompt and is stripped before the turn is recorded. If either half of that stops
being true the feature is either useless (the model never learns what the command meant)
or corrosive (an old expansion replays forever, and a reviewer reads repo-authored text
under the operator's own label).
"""

from __future__ import annotations

import httpx
import pytest
from pydantic import JsonValue

from routes.deps import app_state

from ._helpers import client_app, collect_sse_events, patch_model_resolution
from ._json import arr, json_body, obj, s


async def _turn(
    client: httpx.AsyncClient, prompt: str, command: dict[str, str] | None = None
) -> tuple[list[dict[str, JsonValue]], str]:
    """Run one turn to its end; hand back its events and the thread it landed in."""
    payload: dict[str, object] = {"prompt": prompt}
    if command is not None:
        payload["command"] = command
    resp = await client.post("/chat", json=payload)
    assert resp.status_code == 202, resp.text
    created = json_body(resp)
    return await collect_sse_events(client, s(created["run_id"])), s(created["conversation_id"])


def _injections(events: list[dict[str, JsonValue]]) -> list[dict[str, JsonValue]]:
    return [e for e in events if e["type"] == "context.injected"]


def _command_rows(events: list[dict[str, JsonValue]]) -> list[dict[str, JsonValue]]:
    return [e for e in _injections(events) if e["contributor"] == "command"]


async def _detail(client: httpx.AsyncClient, conversation_id: str) -> dict[str, JsonValue]:
    return json_body(await client.get(f"/conversations/{conversation_id}"))


def _messages(detail: dict[str, JsonValue], role: str) -> list[dict[str, JsonValue]]:
    return [obj(m) for m in arr(detail["messages"]) if obj(m)["role"] == role]


class TestTheExpansionReachesTheModel:
    async def test_an_agent_command_is_announced_as_tail_context(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        patch_model_resolution(monkeypatch)
        async with client_app() as (client, _app):
            events, _ = await _turn(
                client,
                "/reviewer check the auth path",
                {"name": "reviewer", "argument": "check the auth path"},
            )
        rows = _command_rows(events)
        assert len(rows) == 1, "announced exactly once, under a fixed slug"
        # The tail, never the head: a command body differs per turn, and a volatile block
        # at the head invalidates the whole cached prefix behind it.
        assert rows[0]["placement"] == "prompt"
        assert "agents_spawn" in s(rows[0]["text"])
        assert "check the auth path" in s(rows[0]["text"])

    async def test_a_skill_command_directs_the_model_to_open_it(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        patch_model_resolution(monkeypatch)
        async with client_app() as (client, _app):
            created = await client.post(
                "/skills", json={"name": "release-notes", "description": "d", "body": "b"}
            )
            _ = await client.post(f"/skills/{s(json_body(created)['id'])}/publish")
            events, _ = await _turn(
                client, "/release-notes", {"name": "release-notes", "argument": ""}
            )
        row = _command_rows(events)[0]
        assert "skills_open" in s(row["text"])

    async def test_a_turn_with_no_command_announces_none(self, monkeypatch: pytest.MonkeyPatch):
        patch_model_resolution(monkeypatch)
        async with client_app() as (client, _app):
            events, _ = await _turn(client, "say hi")
        assert not _command_rows(events)


class TestWhatIsWrittenDown:
    async def test_the_turn_on_record_is_the_literal_text(self, monkeypatch: pytest.MonkeyPatch):
        patch_model_resolution(monkeypatch)
        async with client_app() as (client, _app):
            _events, conversation_id = await _turn(
                client,
                "/reviewer check the auth path",
                {"name": "reviewer", "argument": "check the auth path"},
            )
            detail = await _detail(client, conversation_id)

        user_turns = _messages(detail, "user")
        assert len(user_turns) == 1
        # Exactly what they typed — not the expansion, and not the two concatenated.
        assert user_turns[0]["content"] == "/reviewer check the auth path"
        assert "agents_spawn" not in s(user_turns[0]["content"])


class TestRerunningTheTurn:
    """A turn sent with a command has to be re-runnable — regenerated, or edited and
    re-asked — and neither path receives one.

    What makes that work is that the **invocation** is written down while the expansion is
    not. The block is built again from whatever the name resolves to at the moment it is
    needed, so a re-run picks up an edited template instead of replaying a copy of the old
    one, and a directive reading "before anything else in this turn" is never left lying in
    history where a later turn would read it.
    """

    async def _sent(self, client: httpx.AsyncClient) -> tuple[str, dict[str, JsonValue]]:
        events, conversation_id = await _turn(
            client,
            "/reviewer check the auth path",
            {"name": "reviewer", "argument": "check the auth path"},
        )
        assert events[-1]["type"] == "run.ended"
        return conversation_id, await _detail(client, conversation_id)

    async def test_a_regenerate_expands_the_command_again(self, monkeypatch: pytest.MonkeyPatch):
        patch_model_resolution(monkeypatch)
        async with client_app() as (client, _app):
            conversation_id, detail = await self._sent(client)
            assistant = _messages(detail, "assistant")[0]
            resp = await client.post(
                "/chat/regenerate",
                json={"conversation_id": conversation_id, "message_id": assistant["id"]},
            )
            assert resp.status_code == 202, resp.text
            events = await collect_sse_events(client, s(json_body(resp)["run_id"]))

        rows = _command_rows(events)
        assert len(rows) == 1, "the second answer is to the same question as the first"
        assert "agents_spawn" in s(rows[0]["text"])
        assert "check the auth path" in s(rows[0]["text"])

    async def test_the_expansion_still_never_lands_in_history(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        patch_model_resolution(monkeypatch)
        async with client_app() as (client, app):
            conversation_id, detail = await self._sent(client)
            assistant = _messages(detail, "assistant")[0]
            resp = await client.post(
                "/chat/regenerate",
                json={"conversation_id": conversation_id, "message_id": assistant["id"]},
            )
            _ = await collect_sse_events(client, s(json_body(resp)["run_id"]))
            replay = await app_state(app).conversations.model_history(conversation_id)

        # The point of stamping the invocation rather than the block: after a regenerate
        # the thread still holds one copy of the operator's typed text and no copy of the
        # directive. A persisted expansion would now be replayed on every later turn.
        assert not any("agents_spawn" in str(m) for m in replay)

    async def test_an_edit_keeps_the_command_its_new_text_still_names(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        patch_model_resolution(monkeypatch)
        async with client_app() as (client, _app):
            conversation_id, detail = await self._sent(client)
            user_turn = _messages(detail, "user")[0]
            resp = await client.post(
                "/chat/edit",
                json={
                    "conversation_id": conversation_id,
                    "message_id": user_turn["id"],
                    # A typo fixed. The inline edit box has no picker, so nothing about the
                    # command comes back from the client — it is carried forward or lost.
                    "prompt": "/reviewer check the auth route",
                },
            )
            assert resp.status_code == 202, resp.text
            events = await collect_sse_events(client, s(json_body(resp)["run_id"]))

        rows = _command_rows(events)
        assert len(rows) == 1
        # The *edited* argument, not the one stamped on the turn being replaced: the block
        # is rebuilt, and what the operator has just written is what they meant.
        assert "check the auth route" in s(rows[0]["text"])

    async def test_an_edit_that_removes_the_token_drops_the_command(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        patch_model_resolution(monkeypatch)
        async with client_app() as (client, _app):
            conversation_id, detail = await self._sent(client)
            user_turn = _messages(detail, "user")[0]
            resp = await client.post(
                "/chat/edit",
                json={
                    "conversation_id": conversation_id,
                    "message_id": user_turn["id"],
                    "prompt": "actually, just tell me what the auth path does",
                },
            )
            events = await collect_sse_events(client, s(json_body(resp)["run_id"]))

        # Deleting the token is how the operator says they no longer meant it — the text
        # is the turn of record, on the way out of the composer and on the way back in.
        assert not _command_rows(events)

    async def test_an_edit_to_a_longer_name_drops_the_command(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        patch_model_resolution(monkeypatch)
        async with client_app() as (client, _app):
            # A real command whose name is a prefix of the built-in `reviewer` sub-agent's.
            _ = await client.post(
                "/commands/workflows",
                json={"name": "review", "body": "Read it and say what is wrong."},
            )
            # Sent as `/review …`; edited to `/reviewer …`, which is a *different* name.
            events, conversation_id = await _turn(
                client, "/review the auth path", {"name": "review", "argument": "the auth path"}
            )
            assert events[-1]["type"] == "run.ended"
            detail = await _detail(client, conversation_id)
            user_turn = _messages(detail, "user")[0]
            resp = await client.post(
                "/chat/edit",
                json={
                    "conversation_id": conversation_id,
                    "message_id": user_turn["id"],
                    "prompt": "/reviewer the auth path",
                },
            )
            events = await collect_sse_events(client, s(json_body(resp)["run_id"]))

        # A prefix match would keep `review` and slice its argument at the wrong place,
        # handing the template `er the auth path` — text nobody wrote. The name has to end
        # where the token does.
        rows = _command_rows(events)
        assert not any("er the auth path" in s(row["text"]) for row in rows)

    async def test_a_plain_turn_carries_no_stamp_to_rerun(self, monkeypatch: pytest.MonkeyPatch):
        patch_model_resolution(monkeypatch)
        async with client_app() as (client, app):
            _events, conversation_id = await _turn(client, "say hi")
            stamps = await app_state(app).conversations.turn_stamps(conversation_id)
        # The overwhelmingly common case, and the one a defensive read has to get right:
        # every turn recorded before any of this existed reads as "no command".
        assert stamps.command is None


class TestResolutionFailure:
    async def test_an_unknown_command_still_sends_the_turn(self, monkeypatch: pytest.MonkeyPatch):
        patch_model_resolution(monkeypatch)
        async with client_app() as (client, _app):
            # The catalog narrows on facts that move between the picker's read and the
            # send. Failing the turn would throw away the operator's message to punish
            # them for a race they did not cause.
            events, _ = await _turn(
                client, "/gone now what", {"name": "gone", "argument": "now what"}
            )
        assert events[-1]["type"] == "run.ended"
        assert not _command_rows(events)

    async def test_a_command_the_thread_is_not_offered_expands_to_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        patch_model_resolution(monkeypatch)
        async with client_app() as (client, _app):
            # `compact` needs a conversation to act on; it is also an action, which never
            # composes a turn at all. Either way there is nothing to put in the tail.
            events, _ = await _turn(client, "/compact", {"name": "compact", "argument": ""})
        assert not _command_rows(events)
