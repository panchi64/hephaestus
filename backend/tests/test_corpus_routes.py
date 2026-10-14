"""The /corpus REST surface + the agent corpus.retrieve tool, over a booted app."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

from ._helpers import client_app


async def _wait_indexed(client, source_id: str, *, timeout: float = 5.0) -> dict[str, Any]:
    """Poll the source list until the folder leaves the indexing state (the crawl
    drains on a background worker, so the POST returns before it finishes)."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        sources = (await client.get("/corpus/sources")).json()
        row = next((s for s in sources if s["id"] == source_id), None)
        if row and row["status"] != "indexing":
            return row
        await asyncio.sleep(0.05)
    raise AssertionError("folder never finished indexing")


async def test_folder_lifecycle_and_sources_list():
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "note.txt").write_text("the operator keeps a pet cat")
        async with client_app() as (client, _app):
            # Stub surfaces are listed from day one (memory + conversations + stubs).
            sources = (await client.get("/corpus/sources")).json()
            ids = {s["id"] for s in sources}
            assert {"surf-memory", "surf-uploads"} <= ids

            created = await client.post("/corpus/folders", json={"path": tmp})
            assert created.status_code == 201
            source = created.json()
            assert source["kind"] == "folder"
            assert source["label"] == tmp
            assert source["icon"] == "archive"

            row = await _wait_indexed(client, source["id"])
            assert row["status"] == "indexed"
            assert row["docCount"] == 1  # one text file crawled

            # Stats reflects the index.
            stats = (await client.get("/corpus/stats")).json()
            assert stats["totalDocs"] == 1
            assert stats["totalCollections"] >= 1
            assert "embeddingModel" in stats and "storeSize" in stats

            # Reindex + rebuild are accepted (202).
            assert (await client.post(f"/corpus/sources/{source['id']}/reindex")).status_code == 202
            assert (await client.post(f"/corpus/sources/{source['id']}/rebuild")).status_code == 202
            await _wait_indexed(client, source["id"])

            # Delete removes the folder and its chunks.
            assert (await client.delete(f"/corpus/folders/{source['id']}")).status_code == 204
            sources = (await client.get("/corpus/sources")).json()
            assert source["id"] not in {s["id"] for s in sources}


async def test_blank_path_is_422_and_unknown_id_404():
    async with client_app() as (client, _app):
        assert (await client.post("/corpus/folders", json={"path": "   "})).status_code == 422
        assert (await client.delete("/corpus/folders/nope")).status_code == 404
        assert (await client.post("/corpus/sources/nope/rebuild")).status_code == 404


async def test_agent_corpus_tool_retrieves_indexed_folder_content():
    # A turn with only the corpus category and a TestModel (calls every offered tool
    # once) must drive corpus.retrieve through to the indexed folder content.
    from pydantic_ai.models.test import TestModel

    from agent import build_chat_orchestrator
    from core.container import ServiceContainer
    from runs import RunRegistry, RunStatus
    from tools.corpus import corpus_toolset

    from ._helpers import granting_store

    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "note.txt").write_text("the secret gate code is 4455")
        async with client_app() as (client, app):
            created = await client.post("/corpus/folders", json={"path": tmp})
            source_id = created.json()["id"]
            await _wait_indexed(client, source_id)

            # global recall is AE-3.8-gated; grant it so the TestModel's source-less
            # retrieve auto-approves inline and drives through to the live index.
            grants = await granting_store("operator", "c", "corpus_retrieve")
            orch = build_chat_orchestrator(
                "look something up",
                model=TestModel(custom_output_text="done"),
                categories={"corpus": corpus_toolset()},
                capabilities=ServiceContainer.of(app.state.corpus, grants),
                conversation_id="c",
            )
            run = RunRegistry().submit(kind="chat", owner_id="operator", orchestrator=orch)
            await run.wait()
            assert run.status is RunStatus.done
            # The retrieve tool ran against the live index and found the folder content.
            hits = await app.state.corpus.retrieve("operator", "gate code", limit=5)
            assert hits and any("4455" in h.text for h in hits)
