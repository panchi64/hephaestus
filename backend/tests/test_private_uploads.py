"""Files attached in private chats: which are private, and who may reach them."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import cast

import pytest
from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from core.container import ServiceContainer
from routes.deps import app_state
from runs import Run, RunStream
from services.conversations import ConversationStore
from services.corpus import CorpusChunkStore, CorpusIndex, FolderAdapter
from services.corpus.uploads import UploadsAdapter
from services.private_area import PrivateArea
from services.private_uploads import UploadPrivacy
from services.registry import ModelRegistry
from services.settings_store import SettingsStore
from services.upload_extraction import BasicExtractor
from services.uploads import UploadStore
from tools.attachments import attachments_toolset
from tools.corpus import CorpusPassages, corpus_toolset
from tools.deps import RunDeps
from tools.platform import PlatformDocument, PlatformResults, platform_toolset

from ._helpers import client_app, collect_sse_events, patch_model_resolution
from ._json import b, json_body, json_list, obj, s
from .test_conversation_search import DegradedEmbedder, setup_search
from .test_uploads_extraction import NoVisionOCR

OWNER = "operator"
PIN = "4821"
SECRET = b"salary statement zebrafinch 98000"


@dataclass
class Env:
    store: ConversationStore
    uploads: UploadStore
    adapter: UploadsAdapter
    index: CorpusIndex
    area: PrivateArea
    privacy: UploadPrivacy

    def ctx(self, conversation_id: str) -> RunContext[RunDeps]:
        run = Run(id="r1", kind="chat", owner_id=OWNER, stream=RunStream())
        caps = ServiceContainer.of(self.index, self.area, self.uploads)
        deps = RunDeps(run=run, owner_id=OWNER, conversation_id=conversation_id, caps=caps)
        return RunContext(deps=deps, model=TestModel(), usage=RunUsage(), tool_call_approved=True)

    async def upload(self, *, private: bool = False) -> str:
        view, _ = await self.uploads.create(OWNER, "pay.txt", "text/plain", SECRET, private=private)
        await self.uploads.flush()
        await self.adapter.flush()
        return view.id

    async def chat(self, upload_id: str, *, private: bool) -> str:
        """A chat that was sent this file, settled as the engine does once it records the
        turn, before the drainer writes it down."""
        conversation_id = await self.store.create_conversation(OWNER, private=private)
        turn: list[ModelMessage] = [
            ModelRequest(parts=[UserPromptPart(content="see the file")]),
            ModelResponse(parts=[TextPart(content="seen")], model_name="m"),
        ]
        _ = self.store.record(conversation_id, turn, attachment_ids=[upload_id])
        await self.privacy.reconcile(OWNER, [upload_id])
        await self.store.flush()
        return conversation_id

    async def read(self, conversation_id: str, upload_id: str) -> PlatformDocument | str:
        return cast(
            PlatformDocument | str,
            await platform_toolset()
            .tools["read"]
            .function(self.ctx(conversation_id), f"{upload_id}:{upload_id}#0"),
        )

    async def is_private(self, upload_id: str) -> bool:
        return (await self.uploads.get(OWNER, upload_id)).private

    async def search_files(self, conversation_id: str) -> list[str]:
        result = cast(
            PlatformResults,
            await platform_toolset()
            .tools["search"]
            .function(self.ctx(conversation_id), "zebrafinch", sources=["files"]),
        )
        return [hit.ref for hit in result.hits]

    async def retrieve(self, conversation_id: str, upload_id: str) -> list[str]:
        result = cast(
            CorpusPassages,
            await corpus_toolset()
            .tools["retrieve"]
            .function(self.ctx(conversation_id), "zebrafinch", [upload_id]),
        )
        return [passage.source for passage in result.passages]

    async def provision(self, conversation_id: str, upload_id: str) -> dict[str, object]:
        return cast(
            dict[str, object],
            await attachments_toolset()
            .tools["provision"]
            .function(self.ctx(conversation_id), upload_id),
        )


@pytest.fixture
async def env() -> AsyncIterator[Env]:
    engine, vault, store, _search = await setup_search(DegradedEmbedder())
    embedder = DegradedEmbedder()
    chunks = CorpusChunkStore(engine, vault, embedder)
    folder = FolderAdapter(engine, chunks, vault.unlocked_event)
    index = CorpusIndex(embedder, ModelRegistry.__new__(ModelRegistry), chunks, folder)
    adapter = UploadsAdapter(engine, chunks, vault.unlocked_event)
    index.register(adapter)
    await adapter.start()
    uploads = UploadStore(engine, vault, adapter, BasicExtractor(NoVisionOCR()))
    await uploads.start()
    area = PrivateArea(vault, SettingsStore(engine), store)
    await area.set_pin(OWNER, PIN)
    yield Env(store, uploads, adapter, index, area, UploadPrivacy(uploads, store))
    await uploads.stop()
    await adapter.stop()
    await store.stop()


# --- which files are private --------------------------------------------------------------


async def test_a_file_sent_only_in_private_chats_is_private(env: Env):
    upload = await env.upload()
    _ = await env.chat(upload, private=True)
    assert await env.is_private(upload)


async def test_sending_a_private_file_in_an_ordinary_chat_makes_it_ordinary(env: Env):
    upload = await env.upload(private=True)
    _ = await env.chat(upload, private=True)
    _ = await env.chat(upload, private=False)
    assert not await env.is_private(upload)


async def test_moving_a_chat_carries_its_files_unless_another_private_chat_holds_them(env: Env):
    shared = await env.upload()
    first = await env.chat(shared, private=False)
    moved = await env.area.set_private(OWNER, first, True)
    await env.privacy.reconcile_conversations(OWNER, moved)
    assert await env.is_private(shared)

    _ = await env.chat(shared, private=True)
    moved = await env.area.set_private(OWNER, first, False)
    await env.privacy.reconcile_conversations(OWNER, moved)
    assert not await env.is_private(shared)


async def test_deleting_the_ordinary_chat_that_shared_a_file_makes_it_private_again(env: Env):
    upload = await env.upload(private=True)
    _ = await env.chat(upload, private=True)
    ordinary = await env.chat(upload, private=False)
    assert not await env.is_private(upload)
    await env.store.delete_conversation(ordinary)
    await env.privacy.reconcile(OWNER, [upload])
    assert await env.is_private(upload)


async def test_a_duplicate_keeps_its_flag_until_a_send_settles_it(env: Env):
    upload = await env.upload(private=True)
    assert await env.upload() == upload
    assert await env.is_private(upload)  # staged in an ordinary composer, never sent
    _ = await env.chat(upload, private=False)
    assert not await env.is_private(upload)
    _ = await env.upload(private=True)
    assert not await env.is_private(upload)


# --- who reaches them ----------------------------------------------------------------------


async def test_a_private_file_never_reaches_global_recall(env: Env):
    upload = await env.upload()
    private_chat = await env.chat(upload, private=True)
    elsewhere = await env.store.create_conversation(OWNER)
    assert await env.search_files(elsewhere) == []
    env.area.lock()
    assert await env.search_files(elsewhere) == []
    assert await env.search_files(private_chat) == []
    assert await env.index.read(OWNER, f"{upload}:pay.txt") is None


async def test_an_ordinary_file_is_found(env: Env):
    upload = await env.upload()
    elsewhere = await env.store.create_conversation(OWNER)
    assert await env.search_files(elsewhere) != []
    _ = await env.chat(upload, private=True)
    _ = await env.chat(upload, private=False)
    assert await env.search_files(elsewhere) != []


async def test_only_a_private_chat_opens_its_private_files_even_while_locked(env: Env):
    upload = await env.upload()
    private_chat = await env.chat(upload, private=True)
    ordinary = await env.store.create_conversation(OWNER)
    env.area.lock()

    assert await env.retrieve(ordinary, upload) == []
    assert await env.retrieve(private_chat, upload) == [upload]

    with pytest.raises(ModelRetry, match="no attachment with id"):
        _ = await env.provision(ordinary, upload)
    # Past the privacy check, to the missing workspace.
    assert (await env.provision(private_chat, upload))["ok"] is False

    assert isinstance(await env.read(private_chat, upload), PlatformDocument)
    assert isinstance(await env.read(ordinary, upload), str)


async def test_a_private_chat_cannot_open_another_private_chats_file(env: Env):
    upload = await env.upload()
    _ = await env.chat(upload, private=True)
    other = await env.store.create_conversation(OWNER, private=True)
    assert await env.retrieve(other, upload) == []
    assert isinstance(await env.read(other, upload), str)
    with pytest.raises(ModelRetry, match="no attachment with id"):
        _ = await env.provision(other, upload)


async def test_a_spawned_thread_opens_its_parents_private_files(env: Env):
    upload = await env.upload()
    parent = await env.chat(upload, private=True)
    child = await env.store.create_conversation(OWNER, parent_id=parent)
    assert await env.retrieve(child, upload) == [upload]


# --- routes --------------------------------------------------------------------------------


async def test_the_upload_routes_hide_private_files_while_locked():
    async with client_app() as (client, app):
        state = app_state(app)
        _ = await client.post("/private/pin", json={"pin": PIN})
        created = await client.post(
            "/uploads",
            files={"file": ("pay.txt", SECRET, "text/plain")},
            data={"private": "true"},
        )
        upload = s(json_body(created)["id"])
        assert b(json_body(created)["private"])
        plain = s(
            json_body(
                await client.post("/uploads", files={"file": ("a.txt", b"plain", "text/plain")})
            )["id"]
        )

        listed = {
            s(obj(row)["id"]): b(obj(row)["private"])
            for row in json_list(await client.get("/uploads"))
        }
        assert listed == {upload: True, plain: False}
        assert (await client.get(f"/uploads/{upload}/content")).status_code == 200

        _ = await client.post("/private/lock")
        listed_ids = {s(obj(row)["id"]) for row in json_list(await client.get("/uploads"))}
        assert listed_ids == {plain}
        for response in (
            await client.get(f"/uploads/{upload}"),
            await client.get(f"/uploads/{upload}/content"),
            await client.delete(f"/uploads/{upload}"),
        ):
            assert response.status_code == 403
            assert json_body(response) == {"detail": "private_locked"}

        # The same bytes, or a new private file, are refused without the private row's
        # metadata, and a private id can't be sent into an ordinary chat.
        for response in (
            await client.post("/uploads", files={"file": ("x.txt", SECRET, "text/plain")}),
            await client.post(
                "/uploads",
                files={"file": ("n.txt", b"new", "text/plain")},
                data={"private": "true"},
            ),
        ):
            assert json_body(response) == {"detail": "private_locked"}
        sent = await client.post("/chat", json={"prompt": "hi", "attachment_ids": [upload]})
        assert sent.status_code == 404
        assert (await state.uploads.get(OWNER, upload)).private

        chat = await state.conversations.create_conversation(OWNER)
        _ = state.conversations.record(
            chat,
            [ModelRequest(parts=[UserPromptPart(content="file")])],
            attachment_ids=[upload],
        )
        _ = await client.post("/private/unlock", json={"pin": PIN})
        moved = await client.put(f"/conversations/{chat}/private", json={"private": True})
        assert moved.status_code == 200
        assert (await state.uploads.get(OWNER, upload)).private
        _ = await client.put(f"/conversations/{chat}/private", json={"private": False})
        assert not (await state.uploads.get(OWNER, upload)).private


async def test_a_turn_settles_its_files_once_it_records_them(monkeypatch: pytest.MonkeyPatch):
    patch_model_resolution(monkeypatch)
    async with client_app() as (client, app):
        state = app_state(app)
        _ = await client.post("/private/pin", json={"pin": PIN})
        upload = s(
            json_body(
                await client.post("/uploads", files={"file": ("pay.txt", SECRET, "text/plain")})
            )["id"]
        )
        sent = json_body(
            await client.post(
                "/chat", json={"prompt": "look", "attachment_ids": [upload], "private": True}
            )
        )
        _ = await collect_sse_events(client, s(sent["run_id"]))
        assert (await state.uploads.get(OWNER, upload)).private

        sent = json_body(
            await client.post("/chat", json={"prompt": "again", "attachment_ids": [upload]})
        )
        _ = await collect_sse_events(client, s(sent["run_id"]))
        assert not (await state.uploads.get(OWNER, upload)).private
