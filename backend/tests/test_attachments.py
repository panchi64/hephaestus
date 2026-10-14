"""Chat file attachments + the retroactive knowledge-base exclude toggle.

Covers the two halves of the feature: an attached file is **staged into the conversation's
sandbox** and announced to the model as a short marker naming its path (so the model reads
and pages through the file itself instead of receiving its text — an image being the one
exception, still handed over as pixels), with `attachments_provision` as the re-stage path
for a recycled session; and an enrolled upload can be scoped out of the corpus
retroactively, dropping it from every retrieve.
"""

from __future__ import annotations

import hashlib
import tempfile
from collections.abc import AsyncIterator, Callable, Sequence
from pathlib import Path

import httpx
import pytest
from pydantic import JsonValue
from pydantic_ai import Agent, BinaryContent, DeferredToolRequests
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy import Engine
from sqlmodel import Session, select

from agent import build_chat_orchestrator
from agent.attachments import resolve_attachments
from core.container import ServiceContainer
from core.db import in_session, init_db, make_engine
from core.vault import Vault
from models.corpus import CorpusChunk
from models.upload import Upload, UploadStatus
from runs import Run, RunRegistry, RunStatus, RunStream
from services.conversations import ConversationStore, install_persisted_attachments
from services.corpus.chunk_store import CorpusChunkStore
from services.corpus.folder import FolderAdapter
from services.corpus.index import CorpusIndex
from services.corpus.uploads import UploadsAdapter
from services.registry import ModelRegistry
from services.sandbox import SandboxError, SandboxSessionManager
from services.upload_extraction import BasicExtractor
from services.uploads import UploadStore
from services.workspace import RunWorkspace
from tools import RunDeps, build_agent_toolsets
from tools.attachments import attachments_toolset

from ._helpers import client_app, collect_sse_events, patch_model_resolution
from ._json import arr, json_body, obj, parse_json, s
from .test_memory import FakeEmbedder
from .test_uploads_extraction import NoVisionOCR

OWNER = "operator"


# --- fixtures ---------------------------------------------------------------


async def _uploads_store() -> tuple[Engine, Vault, CorpusChunkStore, UploadsAdapter, UploadStore]:
    """An UploadStore + its corpus adapter over a throwaway in-memory DB. Workers are NOT
    started here — tests that index/drain start (and stop) them explicitly, so no drainer
    task is left pending at teardown. Resolve-only tests never need a worker."""
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(Path(tempfile.mkdtemp()) / "keyfile.json")
    await vault.setup("pw")
    chunk_store = CorpusChunkStore(engine, vault, FakeEmbedder())
    adapter = UploadsAdapter(engine, chunk_store, vault.unlocked_event)
    store = UploadStore(engine, vault, adapter, BasicExtractor(NoVisionOCR()))
    return engine, vault, chunk_store, adapter, store


async def _insert_upload(
    engine: Engine,
    vault: Vault,
    *,
    mime: str,
    text: str | None = "the operator's secret zebra dossier",
    status: str = UploadStatus.DONE,
    content: bytes = b"raw-bytes",
) -> str:
    """Insert an Upload row directly, for precise control over mime/status/text."""

    def work(session: Session) -> str:
        upload = Upload(
            owner_id=OWNER,
            filename_enc=vault.encrypt_str("dossier"),
            mime=mime,
            size_bytes=len(content),
            sha256=hashlib.sha256(content + mime.encode()).hexdigest(),
            blob_enc=vault.encrypt_bytes(content),
            status=status,
            extracted_text_enc=vault.encrypt_str(text) if text else None,
            has_text=bool(text),
        )
        session.add(upload)
        session.flush()
        return upload.id

    return await in_session(engine, work)


# --- the sandbox fakes both halves of the feature stage into ----------------


class _FakeSandboxSession:
    """Records files staged into its (host-side) workspace. Mirrors the real
    ``SandboxSession``'s ``read_file``/``write_file`` contract closely enough to exercise
    the collision-safe staging path (``services.sandbox.staging``)."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    def write_file(self, relpath: str, content: bytes) -> None:
        self.files[relpath] = content

    def read_file(self, relpath: str) -> bytes:
        if relpath not in self.files:
            raise SandboxError(f"no such file in the sandbox: {relpath!r}")
        return self.files[relpath]

    def ensure_workspace(self) -> Path:
        return Path("/fake-host-workspace")


class _FakeSandboxSessions:
    def __init__(self) -> None:
        self.session: _FakeSandboxSession = _FakeSandboxSession()
        self.acquired: str | None = None
        self.holder: object = None

    async def acquire(self, key: str, *, holder: object = None) -> _FakeSandboxSession:
        self.acquired = key
        self.holder = holder
        return self.session


def _workspace(sessions: _FakeSandboxSessions) -> RunWorkspace:
    """The resolved workspace a chat turn would hand `resolve_attachments`. ``None`` in
    its place is the degrade case — a fail-closed sandbox, a locked vault, a workspace
    that won't open — and staging must then fall back to inline text rather than name a
    path that isn't there."""
    return RunWorkspace(root=Path("/fake-host-workspace"), kind="sandbox", files=sessions.session)


# --- resolve_attachments: staged to the sandbox, announced by marker --------

KEY = "conv-1"


def _text(parts: Sequence[object]) -> str:
    return "".join(p for p in parts if isinstance(p, str))


def _binaries(parts: Sequence[object]) -> list[BinaryContent]:
    return [p for p in parts if isinstance(p, BinaryContent)]


async def test_a_document_becomes_a_marker_not_its_text():
    engine, vault, _chunks, _adapter, store = await _uploads_store()
    uid = await _insert_upload(engine, vault, mime="text/plain")
    sessions = _FakeSandboxSessions()

    resolved = await resolve_attachments(
        store, OWNER, [uid], vision=False, workspace=_workspace(sessions)
    )

    # The original bytes are in the run's workspace, under the sanitized basename.
    # *Which* workspace is `services/workspace.py`'s answer, not this function's — see
    # `test_workspace.py` for the assertion that a conversation gets its own.
    assert sessions.session.files == {"attachments/dossier": b"raw-bytes"}
    # And what the model gets is a marker naming it — no extracted text at all, in
    # either shape. The two collapse to the same thing for a document.
    body = _text(resolved.content)
    assert resolved.content == resolved.persisted
    assert "zebra dossier" not in body
    assert "dossier" in body and uid in body and "text/plain" in body
    assert "/work/attachments/dossier" in body
    # The path can go stale (sessions are recyclable) — the marker has to say so.
    assert "attachments_provision" in body and "corpus_retrieve" in body


async def test_vision_model_gets_pixels_and_the_image_is_staged_too():
    engine, vault, _chunks, _adapter, store = await _uploads_store()
    uid = await _insert_upload(engine, vault, mime="image/png")
    sessions = _FakeSandboxSessions()

    resolved = await resolve_attachments(
        store, OWNER, [uid], vision=True, workspace=_workspace(sessions)
    )

    # An image is still pixels, live and retained — there is nothing to page through in a
    # picture — but its bytes are staged as well, so code can act on it with no round-trip.
    assert _binaries(resolved.content)
    assert _binaries(resolved.persisted)
    assert sessions.session.files == {"attachments/dossier": b"raw-bytes"}
    assert "/work/attachments/dossier" in _text(resolved.persisted)


async def test_an_image_for_a_text_only_model_is_a_staged_file_like_any_other():
    engine, vault, _chunks, _adapter, store = await _uploads_store()
    uid = await _insert_upload(engine, vault, mime="image/png")
    sessions = _FakeSandboxSessions()

    resolved = await resolve_attachments(
        store, OWNER, [uid], vision=False, workspace=_workspace(sessions)
    )

    assert not _binaries(resolved.content)
    assert "/work/attachments/dossier" in _text(resolved.content)


async def test_staging_failure_degrades_to_the_full_text_inline():
    engine, vault, _chunks, _adapter, store = await _uploads_store()
    uid = await _insert_upload(engine, vault, mime="text/plain")

    resolved = await resolve_attachments(store, OWNER, [uid], vision=False, workspace=None)

    # No path to point at, so the text rides inline — in full, wrapped as data — and the
    # marker says the file could not be staged rather than naming a path that isn't there.
    body = _text(resolved.content)
    assert resolved.content == resolved.persisted
    assert "zebra dossier" in body
    assert "untrusted" in body.lower()  # wrap_untrusted sentinel/instruction present
    assert "could not be staged" in body
    assert "/work/attachments" not in body


async def test_no_sandbox_wired_degrades_the_same_way():
    engine, vault, _chunks, _adapter, store = await _uploads_store()
    uid = await _insert_upload(engine, vault, mime="text/plain")

    resolved = await resolve_attachments(store, OWNER, [uid], vision=False)

    body = _text(resolved.content)
    assert "zebra dossier" in body and "could not be staged" in body


async def test_still_processing_attachment_is_staged_by_its_bytes():
    # Extraction is what's pending, not the file: the original bytes exist from the moment
    # of upload, so the agent can already read them from its computer.
    engine, vault, _chunks, _adapter, store = await _uploads_store()
    uid = await _insert_upload(
        engine, vault, mime="application/pdf", text=None, status=UploadStatus.EXTRACTING
    )
    sessions = _FakeSandboxSessions()

    resolved = await resolve_attachments(
        store, OWNER, [uid], vision=False, workspace=_workspace(sessions)
    )

    assert sessions.session.files == {"attachments/dossier": b"raw-bytes"}
    assert "/work/attachments/dossier" in _text(resolved.content)


async def test_still_processing_attachment_without_a_sandbox_yields_a_placeholder():
    engine, vault, _chunks, _adapter, store = await _uploads_store()
    uid = await _insert_upload(
        engine, vault, mime="application/pdf", text=None, status=UploadStatus.EXTRACTING
    )

    resolved = await resolve_attachments(store, OWNER, [uid], vision=False)

    assert "still being processed" in _text(resolved.content)


async def test_unknown_attachment_id_is_skipped():
    _engine, _vault, _chunks, _adapter, store = await _uploads_store()
    resolved = await resolve_attachments(
        store, OWNER, ["nope"], vision=True, workspace=_workspace(_FakeSandboxSessions())
    )
    assert resolved.content == [] and resolved.persisted == [] and resolved.ids == []


async def test_two_files_sharing_a_basename_stage_to_distinct_paths():
    engine, vault, _chunks, _adapter, store = await _uploads_store()
    ids = [
        await _insert_upload(engine, vault, mime="text/plain", content=f"c{n}".encode())
        for n in range(2)
    ]
    sessions = _FakeSandboxSessions()

    resolved = await resolve_attachments(
        store, OWNER, ids, vision=False, workspace=_workspace(sessions)
    )

    assert set(sessions.session.files) == {"attachments/dossier", "attachments/dossier-2"}
    body = _text(resolved.content)
    assert "/work/attachments/dossier-2" in body


def _counted[**P, R](calls: list[str], name: str, original: Callable[P, R]) -> Callable[P, R]:
    def counted(*args: P.args, **kwargs: P.kwargs) -> R:
        calls.append(name)
        return original(*args, **kwargs)

    return counted


async def test_a_multi_file_turn_reads_the_store_a_fixed_number_of_times(
    monkeypatch: pytest.MonkeyPatch,
):
    # Resolution used to call `get` (and, for an image, `content`) per attachment, each a
    # thread hop into the DB — a four-file turn cost eight. The count must not scale with
    # the number of files.
    engine, vault, _chunks, _adapter, store = await _uploads_store()
    ids = [
        await _insert_upload(engine, vault, mime="image/png", content=f"px{n}".encode())
        for n in range(4)
    ]
    calls: list[str] = []
    monkeypatch.setattr(store, "get", _counted(calls, "get", store.get))
    monkeypatch.setattr(store, "content", _counted(calls, "content", store.content))
    monkeypatch.setattr(store, "get_many", _counted(calls, "get_many", store.get_many))
    monkeypatch.setattr(store, "contents", _counted(calls, "contents", store.contents))

    resolved = await resolve_attachments(
        store, OWNER, ids, vision=True, workspace=_workspace(_FakeSandboxSessions())
    )

    assert resolved.ids == ids  # order preserved, every file resolved
    assert len(_binaries(resolved.content)) == 4
    assert calls == ["get_many", "contents"]  # two reads, not two per file


async def test_resolution_order_follows_the_request_not_the_database():
    # The batch read returns rows in whatever order SQLite hands them back; the marker
    # must still follow the order the operator attached them in.
    engine, vault, _chunks, _adapter, store = await _uploads_store()
    ids = [
        await _insert_upload(
            engine, vault, mime="text/plain", text=f"file-{n}", content=f"c{n}".encode()
        )
        for n in range(3)
    ]
    reversed_ids = list(reversed(ids))

    resolved = await resolve_attachments(
        store, OWNER, reversed_ids, vision=False, workspace=_workspace(_FakeSandboxSessions())
    )

    assert resolved.ids == reversed_ids
    body = _text(resolved.content)
    assert body.index(reversed_ids[0]) < body.index(reversed_ids[1]) < body.index(reversed_ids[2])


# --- install_persisted_attachments: what the durable blob carries -----------


def test_install_keeps_the_prompt_and_retains_an_image():
    request = ModelRequest(
        parts=[
            UserPromptPart(
                content=["summarize this", BinaryContent(data=b"x", media_type="image/png")]
            )
        ]
    )
    install_persisted_attachments(
        request,
        [BinaryContent(data=b"x", media_type="image/png"), "[Attached file(s): a.png (id: up1).]"],
    )

    part = request.parts[0]
    assert isinstance(part, UserPromptPart)
    assert isinstance(part.content, list)  # an image survives → kept as a multimodal list
    assert part.content[0] == "summarize this"
    assert any(isinstance(item, BinaryContent) for item in part.content)
    assert any(isinstance(item, str) and "Attached file(s)" in item for item in part.content)


def test_install_collapses_to_text_when_no_binary_survives():
    request = ModelRequest(parts=[UserPromptPart(content=["summarize this", "<file>doc</file>"])])
    install_persisted_attachments(request, ["<file>doc</file>", "[Attached file(s): d (id: up2).]"])

    part = request.parts[0]
    assert isinstance(part, UserPromptPart)
    assert isinstance(part.content, str)  # no binary → collapsed back to one string
    assert part.content.startswith("summarize this")
    assert "Attached file(s)" in part.content


# --- the engine: full live content, capped persisted content ----------------


async def _conv_store() -> ConversationStore:
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(Path(tempfile.mkdtemp()) / "keyfile.json")
    await vault.setup("pw")
    return ConversationStore(engine, vault, FakeEmbedder())


async def test_image_attachment_is_retained_inline_in_history():
    # The file is handed to the model for this turn (pixels, vision=True), and — being an
    # image — it is *retained inline* in replayed history so a later turn re-sees it.
    up_engine, vault, _c, _a, uploads = await _uploads_store()
    uid = await _insert_upload(up_engine, vault, mime="image/png")

    conv = await _conv_store()
    await conv.start()
    cid = await conv.create_conversation(OWNER)

    # Snapshot what the model received *at call time* — the message object is mutated in
    # place when the capped content is installed afterward.
    saw_binary: list[bool] = []

    async def capture(messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str]:
        # The engine streams the turn, so a stream_function is what FunctionModel needs.
        user = next(p for m in messages for p in m.parts if isinstance(p, UserPromptPart))
        content = user.content if isinstance(user.content, list) else [user.content]
        saw_binary.append(any(isinstance(item, BinaryContent) for item in content))
        yield "looked at it"

    orch = build_chat_orchestrator(
        "what is this?",
        model=FunctionModel(stream_function=capture),
        store=conv,
        conversation_id=cid,
        uploads=uploads,
        attachment_ids=[uid],
        vision=True,
    )
    run = RunRegistry().submit(kind="chat", owner_id=OWNER, orchestrator=orch)
    await run.wait()
    assert run.status is RunStatus.done
    assert saw_binary == [True]  # the model saw the actual image this turn

    # The persisted history keeps the image inline (multimodal list) plus the prompt and
    # the id marker — no re-fetch needed on a later turn.
    history = await conv.history(cid)
    content = next(p.content for p in history[0].parts if isinstance(p, UserPromptPart))
    assert isinstance(content, list)
    assert any(isinstance(item, BinaryContent) for item in content)
    text = " ".join(item for item in content if isinstance(item, str))
    assert "what is this?" in text and uid in text
    await conv.stop()


# --- attachments_provision: re-stage a file into the sandbox ----------------


def _provision_then_answer(uid: str) -> Callable[[list[ModelMessage], AgentInfo], ModelResponse]:
    """A FunctionModel function: call attachments_provision once, then answer."""

    def fn(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        already = any(
            isinstance(p, ToolReturnPart | RetryPromptPart) for m in messages for p in m.parts
        )
        if already:
            return ModelResponse(parts=[TextPart("done")])
        return ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name="attachments_provision",
                    args={"attachment_id": uid},
                    tool_call_id="c1",
                )
            ]
        )

    return fn


async def _run_provision(
    uid: str, *, uploads: UploadStore | None, sessions: _FakeSandboxSessions | None
) -> tuple[list[ToolReturnPart], list[RetryPromptPart]]:
    agent = Agent(
        FunctionModel(_provision_then_answer(uid)),
        deps_type=RunDeps,
        toolsets=build_agent_toolsets({"attachments": attachments_toolset()}),
        output_type=[str, DeferredToolRequests],
    )
    run = Run(id="t", kind="chat", owner_id=OWNER, stream=RunStream())
    caps = ServiceContainer()
    if uploads is not None:
        caps.add(uploads)
    if sessions is not None:
        # The fake sandbox manager registers under the class the tools resolve.
        caps.add(sessions, as_type=SandboxSessionManager)
    deps = RunDeps(
        run=run,
        owner_id=OWNER,
        caps=caps,
        conversation_id="conv-1",
    )
    result = await agent.run("go", deps=deps)
    returns = [p for m in result.all_messages() for p in m.parts if isinstance(p, ToolReturnPart)]
    retries = [p for m in result.all_messages() for p in m.parts if isinstance(p, RetryPromptPart)]
    return returns, retries


def _payload(ret: ToolReturnPart) -> dict[str, JsonValue]:
    return obj(parse_json(ret.model_response_str()))


async def test_provision_stages_attachment_into_the_sandbox():
    engine, vault, _c, _a, uploads = await _uploads_store()
    uid = await _insert_upload(engine, vault, mime="text/csv", content=b"a,b\n1,2\n")
    sessions = _FakeSandboxSessions()

    returns, _retries = await _run_provision(uid, uploads=uploads, sessions=sessions)

    assert sessions.acquired == "conv-1"  # keyed by the conversation, like code_execute
    assert sessions.session.files["attachments/dossier"] == b"a,b\n1,2\n"
    [ret] = returns
    payload = _payload(ret)
    assert payload["ok"] is True
    assert payload["path"] == "/work/attachments/dossier"
    assert payload["size_bytes"] == len(b"a,b\n1,2\n")


async def test_provision_unknown_id_retries_and_stages_nothing():
    _engine, _v, _c, _a, uploads = await _uploads_store()
    sessions = _FakeSandboxSessions()

    _returns, retries = await _run_provision("ghost", uploads=uploads, sessions=sessions)

    assert retries  # a bad id raises ModelRetry → the model is asked to correct it
    assert sessions.session.files == {}  # nothing staged


async def test_provision_degrades_without_a_sandbox():
    engine, vault, _c, _a, uploads = await _uploads_store()
    uid = await _insert_upload(engine, vault, mime="text/plain")

    returns, _retries = await _run_provision(uid, uploads=uploads, sessions=None)

    [ret] = returns
    payload = _payload(ret)
    assert payload["ok"] is False
    error = payload["error"]
    assert isinstance(error, str)
    assert "unavailable" in error.lower()


# --- sandbox-04: collision-safe staging --------------------------------------


async def test_provision_disambiguates_a_filename_collision():
    # Two distinct attachments both sanitize to "dossier" (_insert_upload's fixed
    # filename) but carry different bytes — the second must not silently clobber
    # the first, and the result must report the actual path it landed at.
    engine, vault, _c, _a, uploads = await _uploads_store()
    uid1 = await _insert_upload(engine, vault, mime="text/csv", content=b"a,b\n1,2\n")
    uid2 = await _insert_upload(engine, vault, mime="text/csv", content=b"c,d\n3,4\n")
    sessions = _FakeSandboxSessions()

    returns1, _r1 = await _run_provision(uid1, uploads=uploads, sessions=sessions)
    returns2, _r2 = await _run_provision(uid2, uploads=uploads, sessions=sessions)

    [ret1] = returns1
    [ret2] = returns2
    payload1 = _payload(ret1)
    payload2 = _payload(ret2)
    assert payload1["path"] == "/work/attachments/dossier"
    assert "renamed" not in payload1

    # The second attachment gets a disambiguated, distinct path — and is told so.
    assert payload2["path"] == "/work/attachments/dossier-2"
    assert payload2["renamed"] is True
    note = payload2["note"]
    assert isinstance(note, str)
    assert "already used this name" in note

    # Both files survive, distinct and intact — neither clobbered the other.
    assert sessions.session.files["attachments/dossier"] == b"a,b\n1,2\n"
    assert sessions.session.files["attachments/dossier-2"] == b"c,d\n3,4\n"


async def test_provision_reprovisioning_the_same_attachment_is_idempotent():
    # Re-provisioning the *same* attachment (same bytes at the same name) is a
    # no-op re-stage, not a collision — it must land back at the original path.
    engine, vault, _c, _a, uploads = await _uploads_store()
    uid = await _insert_upload(engine, vault, mime="text/csv", content=b"a,b\n1,2\n")
    sessions = _FakeSandboxSessions()

    returns1, _r1 = await _run_provision(uid, uploads=uploads, sessions=sessions)
    returns2, _r2 = await _run_provision(uid, uploads=uploads, sessions=sessions)

    assert _payload(returns1[0])["path"] == "/work/attachments/dossier"
    assert _payload(returns2[0])["path"] == "/work/attachments/dossier"
    assert "renamed" not in _payload(returns2[0])
    assert sessions.session.files.keys() == {"attachments/dossier"}


# --- the retroactive knowledge-base exclude toggle --------------------------


async def _drain_adapter(adapter: UploadsAdapter) -> None:
    await adapter.flush()


async def test_excluded_upload_drops_from_retrieve_and_returns_on_reinclude():
    _engine, _vault, _chunks, adapter, _store = await _uploads_store()
    await adapter.start()
    try:
        adapter.index_upload(OWNER, "up-zebra", "the zebra dossier lives here")
        await _drain_adapter(adapter)
        assert await adapter.retrieve(OWNER, "zebra", None, None, {"zebra"}, limit=5)

        adapter.set_excluded(OWNER, "up-zebra", True)
        await _drain_adapter(adapter)
        assert await adapter.retrieve(OWNER, "zebra", None, None, {"zebra"}, limit=5) == []

        adapter.set_excluded(OWNER, "up-zebra", False)
        await _drain_adapter(adapter)
        assert await adapter.retrieve(OWNER, "zebra", None, None, {"zebra"}, limit=5)
    finally:
        await adapter.stop()


async def test_set_kb_excluded_flips_flag_and_restamps_chunks():
    engine, _vault, _chunks, adapter, store = await _uploads_store()
    await store.start()
    await adapter.start()
    view, _created = await store.create(OWNER, "n.txt", "text/plain", b"zebra facts abound here")
    await store.flush()
    await adapter.flush()

    _ = await store.set_kb_excluded(OWNER, view.id, True)
    await adapter.flush()

    refreshed = await store.get(OWNER, view.id)
    assert refreshed.kb_excluded is True
    with Session(engine) as session:
        chunks = session.exec(select(CorpusChunk).where(CorpusChunk.source_id == view.id)).all()
    assert chunks and all(c.kb_excluded for c in chunks)
    assert await adapter.retrieve(OWNER, "zebra", None, None, {"zebra"}, limit=5) == []

    _ = await store.set_kb_excluded(OWNER, view.id, False)
    await adapter.flush()
    assert await adapter.retrieve(OWNER, "zebra", None, None, {"zebra"}, limit=5)
    await store.stop()
    await adapter.stop()


async def test_targeted_source_ids_read_scopes_to_one_file_and_overrides_exclude():
    engine, vault, chunk_store, adapter, _store = await _uploads_store()
    await adapter.start()
    try:
        adapter.index_upload(OWNER, "file-a", "alpha content about otters")
        adapter.index_upload(OWNER, "file-b", "beta content about otters")
        await _drain_adapter(adapter)

        index = CorpusIndex(
            FakeEmbedder(),
            ModelRegistry.__new__(ModelRegistry),
            chunk_store,
            FolderAdapter(engine, chunk_store, vault.unlocked_event),
        )
        hits = await index.retrieve(OWNER, "otters", source_ids=["file-a"], limit=5)
        assert hits and all(h.source_id == "file-a" for h in hits)

        # Excluding scopes the file out of AMBIENT recall (no source_ids)...
        adapter.set_excluded(OWNER, "file-a", True)
        await _drain_adapter(adapter)
        assert await index.retrieve(OWNER, "otters", sources=["uploads"], limit=5) == []
        # ...but an explicit by-id fetch (a chat reading its own attachment) still gets it.
        still = await index.retrieve(OWNER, "otters", source_ids=["file-a"], limit=5)
        assert still and all(h.source_id == "file-a" for h in still)
    finally:
        await adapter.stop()


# --- routes -----------------------------------------------------------------


async def _upload_file(
    client: httpx.AsyncClient,
    name: str = "note.txt",
    body: bytes = b"hello there",
    mime: str = "text/plain",
) -> str:
    resp = await client.post("/uploads", files={"file": (name, body, mime)})
    assert resp.status_code in (200, 201)
    return s(json_body(resp)["id"])


async def test_chat_accepts_attachment_ids_and_turn_carries_them(monkeypatch: pytest.MonkeyPatch):
    patch_model_resolution(monkeypatch)
    async with client_app() as (client, _app):
        uid = await _upload_file(client)
        resp = await client.post(
            "/chat", json={"prompt": "look at my file", "attachment_ids": [uid]}
        )
        assert resp.status_code == 202
        body = json_body(resp)
        _ = await collect_sse_events(client, s(body["run_id"]))

        detail = await client.get(f"/conversations/{s(body['conversation_id'])}")
        messages = arr(json_body(detail)["messages"])
        user_turn = next(obj(m) for m in messages if obj(m)["role"] == "user")
        # The conversations route emits snake_case (MessageOut is a plain BaseModel,
        # unlike the camelCase upload DTOs); the frontend maps it to attachmentIds.
        assert user_turn["attachment_ids"] == [uid]


async def test_chat_rejects_unknown_attachment_id(monkeypatch: pytest.MonkeyPatch):
    patch_model_resolution(monkeypatch)
    async with client_app() as (client, _app):
        resp = await client.post("/chat", json={"prompt": "hi", "attachment_ids": ["ghost"]})
        assert resp.status_code == 404


async def test_chat_allows_empty_prompt_with_an_attachment(monkeypatch: pytest.MonkeyPatch):
    patch_model_resolution(monkeypatch)
    async with client_app() as (client, _app):
        uid = await _upload_file(client)
        resp = await client.post("/chat", json={"prompt": "", "attachment_ids": [uid]})
        assert resp.status_code == 202
        # …but still nothing to act on with neither text nor a file.
        empty = await client.post("/chat", json={"prompt": "   "})
        assert empty.status_code == 422


async def test_patch_upload_toggles_kb_excluded(monkeypatch: pytest.MonkeyPatch):
    patch_model_resolution(monkeypatch)
    async with client_app() as (client, _app):
        uid = await _upload_file(client)
        resp = await client.patch(f"/uploads/{uid}", json={"kbExcluded": True})
        assert resp.status_code == 200
        assert json_body(resp)["kbExcluded"] is True

        back = await client.patch(f"/uploads/{uid}", json={"kbExcluded": False})
        assert json_body(back)["kbExcluded"] is False


def test_provision_says_only_what_the_model_cannot_read_elsewhere():
    """The description used to carry the sandbox's working directory, a pointer at
    `code_execute` and the whole result shape — all of it said better on the tools that
    own it, and re-charged on every turn the attachments category is offered. What is
    left is the recovery it exists for, the path rule, and the other way to read a
    document."""
    description = attachments_toolset().tools["provision"].description or ""

    assert "corpus_retrieve" in description and "files_*" in description
    assert "/work" not in description and "code_execute" not in description
    assert "verbatim" in description  # the rule a name clash makes load-bearing
    assert len(description) < 500
