"""A turn carrying `@` file references: what reaches the model, and what does not.

The claim being defended is that a reference is a **reference**. The turn names paths; it
does not read them. That is what keeps an `@` from twenty turns ago pointing at a file
whose old contents are still being replayed, and it is why the marker is trusted content —
there is no file text in it to be untrusted about.
"""

from __future__ import annotations

from pathlib import Path

from tests._helpers import client_app, collect_sse_events, patch_model_resolution
from tests.test_code_mode import _project


def _injections(events: list[dict], contributor: str) -> list[dict]:
    return [
        e
        for e in events
        if e["type"] == "context.injected" and e["contributor"] == contributor
    ]


async def _project_turn(client, project_id: str, refs: list[str]) -> list[dict]:
    resp = await client.post(
        "/chat",
        json={
            "prompt": "look at these",
            "project_id": project_id,
            "file_refs": refs,
        },
    )
    assert resp.status_code == 202, resp.text
    return await collect_sse_events(client, resp.json()["run_id"])


class TestWhatReachesTheModel:
    async def test_a_reference_names_the_path_and_nothing_else(
        self, monkeypatch, tmp_path: Path
    ):
        patch_model_resolution(monkeypatch)
        async with client_app() as (client, _app):
            project = await _project(client, tmp_path)
            events = await _project_turn(client, project["id"], ["hello.txt"])
        rows = _injections(events, "file_refs")
        assert len(rows) == 1
        assert rows[0]["placement"] == "prompt"
        assert "hello.txt" in rows[0]["text"]
        # The file says "original"; a reference that carried its contents would be an
        # injection, and would go stale in history the moment the file changed.
        assert "original" not in rows[0]["text"]
        assert "files_read_file" in rows[0]["text"]

    async def test_a_path_outside_the_workspace_is_dropped(
        self, monkeypatch, tmp_path: Path
    ):
        patch_model_resolution(monkeypatch)
        async with client_app() as (client, _app):
            project = await _project(client, tmp_path)
            events = await _project_turn(
                client, project["id"], ["../../../etc/passwd", "hello.txt"]
            )
        rows = _injections(events, "file_refs")
        assert "passwd" not in rows[0]["text"]
        assert "hello.txt" in rows[0]["text"]

    async def test_a_turn_naming_only_unresolvable_paths_still_sends(
        self, monkeypatch, tmp_path: Path
    ):
        patch_model_resolution(monkeypatch)
        async with client_app() as (client, _app):
            project = await _project(client, tmp_path)
            events = await _project_turn(client, project["id"], ["does/not/exist.ts"])
        # The operator still has their typed message; refusing the turn over a file they
        # can simply mention again is the worse trade.
        assert events[-1]["type"] == "run.ended"
        assert _injections(events, "file_refs") == []

    async def test_a_turn_with_no_references_announces_nothing(
        self, monkeypatch, tmp_path: Path
    ):
        patch_model_resolution(monkeypatch)
        async with client_app() as (client, _app):
            project = await _project(client, tmp_path)
            events = await _project_turn(client, project["id"], [])
        assert _injections(events, "file_refs") == []


class TestWhatIsWrittenDown:
    """A reference has to outlive the turn that made it — a reload must still show the
    chip, and a regenerate must still tell the model which files were meant.

    It gets that the way an attachment does: the **marker persists** with the prompt. A
    marker is a statement of fact — these paths were referenced — so replaying it is
    honest, where replaying a file's contents would be a copy going stale behind the file.
    That is also what makes a regenerate work with no second mechanism, since the marker
    is already in the history a regenerate replays.
    """

    async def _turn_with_refs(self, client, tmp_path: Path) -> tuple[str, dict]:
        project = await _project(client, tmp_path)
        resp = await client.post(
            "/chat",
            json={
                "prompt": "look at these",
                "project_id": project["id"],
                "file_refs": ["hello.txt"],
            },
        )
        conversation_id = resp.json()["conversation_id"]
        await collect_sse_events(client, resp.json()["run_id"])
        detail = (await client.get(f"/conversations/{conversation_id}")).json()
        return conversation_id, detail

    async def test_the_paths_come_back_for_the_chips(self, monkeypatch, tmp_path: Path):
        patch_model_resolution(monkeypatch)
        async with client_app() as (client, _app):
            _cid, detail = await self._turn_with_refs(client, tmp_path)
        user_turn = next(m for m in detail["messages"] if m["role"] == "user")
        # Structured, not parsed back out of the marker's prose.
        assert user_turn["file_refs"] == ["hello.txt"]

    async def test_the_operators_own_words_are_the_whole_of_their_turn(
        self, monkeypatch, tmp_path: Path
    ):
        patch_model_resolution(monkeypatch)
        async with client_app() as (client, _app):
            _cid, detail = await self._turn_with_refs(client, tmp_path)
        user_turn = next(m for m in detail["messages"] if m["role"] == "user")
        # The marker rides the same request, so it is in the blob the model replays — and
        # it is taken back off on the way to the transcript, because the bubble it would
        # land in carries the operator's name and they did not write it.
        assert user_turn["content"] == "look at these"

    async def test_a_regenerate_still_knows_which_files_were_meant(
        self, monkeypatch, tmp_path: Path
    ):
        patch_model_resolution(monkeypatch)
        async with client_app() as (client, app):
            conversation_id, detail = await self._turn_with_refs(client, tmp_path)
            assistant = next(m for m in detail["messages"] if m["role"] == "assistant")
            resp = await client.post(
                "/chat/regenerate",
                json={"conversation_id": conversation_id, "message_id": assistant["id"]},
            )
            assert resp.status_code == 202, resp.text
            await collect_sse_events(client, resp.json()["run_id"])
            after = (await client.get(f"/conversations/{conversation_id}")).json()
            # What the *model* would be handed, asked of the store directly. A regenerate
            # re-runs from history with no fresh prompt, so the only way the marker can be
            # in that replay is if it persisted with the turn.
            replay = await app.state.conversations.model_history(conversation_id)

        assert any("files_read_file" in str(m) for m in replay)
        # And the chip survives too — it is stamped on the user request, which a
        # regenerate leaves alone while it re-answers beneath it.
        user_turn = next(m for m in after["messages"] if m["role"] == "user")
        assert user_turn["file_refs"] == ["hello.txt"]

    async def test_an_edit_keeps_the_references_its_new_text_still_names(
        self, monkeypatch, tmp_path: Path
    ):
        patch_model_resolution(monkeypatch)
        async with client_app() as (client, _app):
            conversation_id, detail = await self._turn_with_refs(client, tmp_path)
            user_turn = next(m for m in detail["messages"] if m["role"] == "user")
            resp = await client.post(
                "/chat/edit",
                json={
                    "conversation_id": conversation_id,
                    "message_id": user_turn["id"],
                    # The `@` token kept, the rest rewritten. An edit is a fresh request —
                    # nothing of the old one is replayed — so a reference dropped here is
                    # a chip the operator watches vanish for fixing a typo.
                    "prompt": "look at @hello.txt and tell me what it says",
                },
            )
            assert resp.status_code == 202, resp.text
            await collect_sse_events(client, resp.json()["run_id"])
            after = (await client.get(f"/conversations/{conversation_id}")).json()
        edited = next(m for m in after["messages"] if m["role"] == "user")
        assert edited["file_refs"] == ["hello.txt"]

    async def test_an_edit_that_grows_the_path_drops_the_reference(
        self, monkeypatch, tmp_path: Path
    ):
        patch_model_resolution(monkeypatch)
        async with client_app() as (client, _app):
            conversation_id, detail = await self._turn_with_refs(client, tmp_path)
            user_turn = next(m for m in detail["messages"] if m["role"] == "user")
            resp = await client.post(
                "/chat/edit",
                json={
                    "conversation_id": conversation_id,
                    "message_id": user_turn["id"],
                    # `@hello.txt` is a substring of `@hello.txt.bak`, and a bare
                    # `in` test would carry the old reference onto a turn that names a
                    # different file.
                    "prompt": "look at @hello.txt.bak instead",
                },
            )
            await collect_sse_events(client, resp.json()["run_id"])
            after = (await client.get(f"/conversations/{conversation_id}")).json()
        edited = next(m for m in after["messages"] if m["role"] == "user")
        assert edited["file_refs"] == []

    async def test_an_edit_that_removes_the_token_drops_the_reference(
        self, monkeypatch, tmp_path: Path
    ):
        patch_model_resolution(monkeypatch)
        async with client_app() as (client, _app):
            conversation_id, detail = await self._turn_with_refs(client, tmp_path)
            user_turn = next(m for m in detail["messages"] if m["role"] == "user")
            resp = await client.post(
                "/chat/edit",
                json={
                    "conversation_id": conversation_id,
                    "message_id": user_turn["id"],
                    "prompt": "never mind the file, just say hello",
                },
            )
            await collect_sse_events(client, resp.json()["run_id"])
            after = (await client.get(f"/conversations/{conversation_id}")).json()
        edited = next(m for m in after["messages"] if m["role"] == "user")
        assert edited["file_refs"] == []
