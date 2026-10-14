"""The /uploads REST surface + its corpus enrollment, over a booted app.

The booted app wires the built-in extractor (no MinerU on the CI host, no vision
endpoint configured), so these exercise the text/native-PDF paths — enough to cover
the HTTP surface end to end."""

from __future__ import annotations

from fastapi import FastAPI

from core.ratelimit import RateLimiter
from services.corpus.uploads import UploadsAdapter
from services.uploads import UploadStore

from ._helpers import app_state, client_app
from ._json import json_body, json_list, obj, s
from .test_uploads_extraction import text_pdf


async def _await_extraction(app: FastAPI) -> None:
    await app_state(app, "uploads", UploadStore)._worker.join()
    await app_state(app, "corpus_uploads", UploadsAdapter)._worker.join()


async def test_upload_extract_list_download_correct_delete():
    async with client_app() as (client, app):
        created = await client.post(
            "/uploads", files={"file": ("notes.txt", b"zebra knowledge here", "text/plain")}
        )
        assert created.status_code == 201
        up = json_body(created)
        uid = s(up["id"])
        assert up["filename"] == "notes.txt" and up["sizeBytes"] == 20  # camelCase out

        await _await_extraction(app)

        detail = json_body(await client.get(f"/uploads/{uid}"))
        assert detail["status"] == "done"
        assert detail["extractedText"] == "zebra knowledge here"
        assert detail["extractor"] == "basic"

        listed = json_list(await client.get("/uploads"))
        assert [obj(u)["id"] for u in listed] == [uid] and obj(listed[0])["hasText"] is True

        content = await client.get(f"/uploads/{uid}/content")
        assert content.content == b"zebra knowledge here"
        assert "attachment" in content.headers["content-disposition"]

        corrected = await client.patch(f"/uploads/{uid}", json={"text": "fixed text"})
        assert corrected.status_code == 200
        assert json_body(corrected)["extractedText"] == "fixed text"

        assert (await client.delete(f"/uploads/{uid}")).status_code == 204
        assert (await client.get(f"/uploads/{uid}")).status_code == 404


async def test_pdf_text_extracted_via_route():
    async with client_app() as (client, app):
        created = await client.post(
            "/uploads", files={"file": ("doc.pdf", text_pdf("route pdf marker"), "application/pdf")}
        )
        uid = s(json_body(created)["id"])
        await _await_extraction(app)
        text = s(json_body(await client.get(f"/uploads/{uid}"))["extractedText"])
        assert "route pdf marker" in text


async def test_duplicate_returns_200():
    async with client_app() as (client, _app):
        first = await client.post("/uploads", files={"file": ("a.txt", b"same", "text/plain")})
        second = await client.post("/uploads", files={"file": ("a.txt", b"same", "text/plain")})
        assert first.status_code == 201 and second.status_code == 200
        assert json_body(first)["id"] == json_body(second)["id"]


async def test_empty_file_rejected():
    async with client_app() as (client, _app):
        resp = await client.post("/uploads", files={"file": ("e.txt", b"", "text/plain")})
        assert resp.status_code == 422


async def test_rate_limited_returns_429():
    async with client_app() as (client, app):
        # A 1-token bucket that never refills ⇒ the second upload is throttled.
        app.state.upload_rate_limiter = RateLimiter(rate_per_second=0.0, burst=1)
        ok = await client.post("/uploads", files={"file": ("a.txt", b"one", "text/plain")})
        throttled = await client.post("/uploads", files={"file": ("b.txt", b"two", "text/plain")})
        assert ok.status_code == 201 and throttled.status_code == 429
        assert "retry-after" in throttled.headers


async def test_uploads_surface_is_real_not_a_stub():
    async with client_app() as (client, _app):
        sources = json_list(await client.get("/corpus/sources"))
        row = next(obj(src) for src in sources if obj(src)["id"] == "surf-uploads")
        assert row["kind"] == "surface"
        assert row["status"] == "indexed"
        assert row["href"] == "/uploads"
