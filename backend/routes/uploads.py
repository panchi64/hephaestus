"""Uploads surface — accept files, expose their extracted text, serve them back.

Thin pass-throughs to :class:`~services.uploads.UploadStore`: list/get, upload (a
multipart file), download the original bytes, correct the extracted text, retry a
failed extraction, and delete. Content (filename, extracted text) is returned
decrypted — the operator owns it. Out-shapes are camelCase to match the frontend's
``uploads`` seam. File *search* is not here — it flows through ``/corpus`` like every
other source, once a file's text is extracted.

Two protections live at this layer (`UP-1`/`UP-4`), where transport concerns belong:
a per-operator rate limit on the upload endpoint, and a single-file size cap. Duplicate
*recognition* is the store's job; this layer only reports it (200 vs 201).
"""

from __future__ import annotations

import io
import math
from datetime import datetime

from fastapi import APIRouter, File, Header, HTTPException, Request, Response, UploadFile
from PIL import Image, ImageOps, UnidentifiedImageError

from core.config import get_settings
from core.exceptions import NotFoundError, RateLimitedError
from routes import deps
from routes.camel import CamelModel
from routes.deps import OPERATOR_ID
from routes.http import content_disposition
from services.artifacts import guess_content_type
from services.uploads import UploadSummaryView, UploadView

# A bounded set of thumbnail edge sizes, so the endpoint can't be driven to render (and
# cache) an unbounded spread of dimensions. The grid asks for one of these.
_THUMB_SIZES = (128, 256, 512)
_DEFAULT_THUMB = 256

router = APIRouter(prefix="/uploads", tags=["uploads"])


class UploadSummaryOut(CamelModel):
    """A library-list row — no full extracted text, just what the list renders."""

    id: str
    filename: str
    mime: str
    size_bytes: int
    status: str
    vision: bool
    extractor: str | None = None
    has_text: bool
    note: str | None = None
    # Whether the operator has scoped this file out of the knowledge base.
    kb_excluded: bool
    # The operator's star (image uploads).
    favorite: bool
    created_at: datetime
    updated_at: datetime


class UploadOut(CamelModel):
    id: str
    filename: str
    mime: str
    size_bytes: int
    status: str
    vision: bool
    extractor: str | None = None
    extracted_text: str | None = None
    note: str | None = None
    kb_excluded: bool
    favorite: bool
    created_at: datetime
    updated_at: datetime


class UploadPatch(CamelModel):
    """A partial update of an upload. Any field may be sent on its own: ``text`` is an
    operator correction of the extracted text (`UP-2`); ``kbExcluded`` toggles
    knowledge-base membership (retroactive); ``favorite`` toggles the operator's star.
    Sending none is a no-op read."""

    text: str | None = None
    kb_excluded: bool | None = None
    favorite: bool | None = None


def _summary_out(view: UploadSummaryView) -> UploadSummaryOut:
    return UploadSummaryOut(
        id=view.id,
        filename=view.filename,
        mime=view.mime,
        size_bytes=view.size_bytes,
        status=view.status,
        vision=view.vision,
        extractor=view.extractor,
        has_text=view.has_text,
        note=view.note,
        kb_excluded=view.kb_excluded,
        favorite=view.favorite,
        created_at=view.created_at,
        updated_at=view.updated_at,
    )


def _out(view: UploadView) -> UploadOut:
    return UploadOut(
        id=view.id,
        filename=view.filename,
        mime=view.mime,
        size_bytes=view.size_bytes,
        status=view.status,
        vision=view.vision,
        extractor=view.extractor,
        extracted_text=view.extracted_text,
        note=view.note,
        kb_excluded=view.kb_excluded,
        favorite=view.favorite,
        created_at=view.created_at,
        updated_at=view.updated_at,
    )


@router.get("", response_model=list[UploadSummaryOut])
async def list_uploads(request: Request) -> list[UploadSummaryOut]:
    views = await deps.uploads(request).list_uploads(OPERATOR_ID)
    return [_summary_out(v) for v in views]


@router.post("", response_model=UploadOut)
async def create_upload(
    request: Request,
    response: Response,
    file: UploadFile = File(...),  # noqa: B008 — FastAPI's parameter-marker default
) -> UploadOut:
    # UP-4: rate-limit the endpoint to protect the service before doing any work.
    try:
        deps.upload_rate_limiter(request).check(OPERATOR_ID)
    except RateLimitedError as exc:
        retry_after = exc.retry_after_s if math.isfinite(exc.retry_after_s) else 60.0
        raise HTTPException(
            status_code=429,
            detail="too many uploads; slow down",
            headers={"Retry-After": str(int(retry_after) + 1)},
        ) from None

    content = await file.read()
    if not content:
        raise HTTPException(status_code=422, detail="file is empty")
    settings = get_settings()
    if len(content) > settings.upload_max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"file exceeds the {settings.upload_max_bytes}-byte limit",
        )
    filename = file.filename or "upload"
    mime = file.content_type or guess_content_type(filename)
    view, created = await deps.uploads(request).create(OPERATOR_ID, filename, mime, content)
    # 201 for a newly stored file, 200 when an identical one already existed (UP-1).
    response.status_code = 201 if created else 200
    return _out(view)


@router.get("/{upload_id}", response_model=UploadOut)
async def get_upload(upload_id: str, request: Request) -> UploadOut:
    try:
        view = await deps.uploads(request).get(OPERATOR_ID, upload_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="upload not found") from None
    return _out(view)


@router.get("/{upload_id}/content")
async def download_upload(upload_id: str, request: Request) -> Response:
    """Serve the original bytes as an attachment, unconditionally. Chat
    image views read these bytes through the client (``getBlob`` → object URL), where the
    Content-Disposition is moot — so forcing ``attachment`` costs the product nothing and
    keeps a direct navigation to this URL from rendering operator-supplied HTML/SVG inline
    in the authenticated API origin (where embedded scripts would run as the operator)."""
    try:
        blob = await deps.uploads(request).content(OPERATOR_ID, upload_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="upload not found") from None
    return Response(
        content=blob.content,
        media_type=blob.mime,
        headers={
            "Content-Disposition": content_disposition(
                blob.filename, inline=False, fallback="upload"
            ),
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/{upload_id}/thumbnail")
async def thumbnail_upload(
    upload_id: str,
    request: Request,
    size: int = _DEFAULT_THUMB,
    if_none_match: str | None = Header(default=None),
) -> Response:
    """A downscaled preview of an image upload for an image grid (a full multi-MB
    original per tile would be wasteful). The ETag is content-addressed (the upload's
    clear ``sha256`` + the requested size), so a conditional re-request is answered ``304``
    **without** unsealing the bytes — the per-request decrypt + decode is paid only on a
    cold load. Falls back to the original bytes for an image Pillow can't open."""
    if size not in _THUMB_SIZES:
        size = _DEFAULT_THUMB
    store = deps.uploads(request)
    head = await store.head(OPERATOR_ID, upload_id)
    if head is None:
        raise HTTPException(status_code=404, detail="upload not found")
    if not head.mime.startswith("image/"):
        raise HTTPException(status_code=415, detail="not an image")

    etag = f'"{head.sha256}.t{size}"'
    cache = "private, max-age=86400"
    if if_none_match == etag:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": cache})

    blob = await store.content(OPERATOR_ID, upload_id)
    try:
        thumb = _render_thumbnail(blob.content, size)
        media_type = "image/webp"
    except UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError:
        # An image Pillow can't decode (an exotic format, or one so large it trips the
        # decompression-bomb guard) still has its original bytes — serve those so the tile
        # renders rather than breaks; the browser scales it down.
        thumb, media_type = blob.content, blob.mime
    return Response(
        content=thumb,
        media_type=media_type,
        headers={
            "ETag": etag,
            "Cache-Control": cache,
            "X-Content-Type-Options": "nosniff",
        },
    )


def _render_thumbnail(raw: bytes, size: int) -> bytes:
    """Decode ``raw``, normalize orientation, and downscale it to fit within ``size``×``size``
    (aspect preserved), re-encoded as WebP. Honors the source's EXIF orientation (so a phone
    portrait isn't sideways in the grid) and preserves transparency (a transparent PNG keeps
    its alpha, not a black box). Pure CPU over already-decrypted bytes — the caller owns the
    decrypt and the ETag/cache that keep this off the repeat path."""
    with Image.open(io.BytesIO(raw)) as opened:
        # Bake the EXIF rotation into the pixels — WebP won't carry the orientation tag.
        image = ImageOps.exif_transpose(opened)
        has_alpha = image.mode in ("RGBA", "LA", "PA") or (
            image.mode == "P" and "transparency" in image.info
        )
        image = image.convert("RGBA" if has_alpha else "RGB")
        image.thumbnail((size, size))
        out = io.BytesIO()
        image.save(out, format="WEBP", quality=80, method=4)
        return out.getvalue()


@router.patch("/{upload_id}", response_model=UploadOut)
async def patch_upload(upload_id: str, body: UploadPatch, request: Request) -> UploadOut:
    """Correct the extracted text, toggle knowledge-base membership, or both. Each is
    applied independently so the frontend can send just the field it changed."""
    store = deps.uploads(request)
    try:
        view: UploadView | None = None
        if body.text is not None:
            view = await store.correct_text(OPERATOR_ID, upload_id, body.text)
        if body.kb_excluded is not None:
            view = await store.set_kb_excluded(OPERATOR_ID, upload_id, body.kb_excluded)
        if body.favorite is not None:
            view = await store.set_favorite(OPERATOR_ID, upload_id, body.favorite)
        if view is None:
            view = await store.get(OPERATOR_ID, upload_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="upload not found") from None
    return _out(view)


@router.post("/{upload_id}/retry", response_model=UploadOut)
async def retry_upload(upload_id: str, request: Request) -> UploadOut:
    try:
        view = await deps.uploads(request).retry(OPERATOR_ID, upload_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="upload not found") from None
    return _out(view)


@router.delete("/{upload_id}", status_code=204)
async def delete_upload(upload_id: str, request: Request) -> None:
    try:
        await deps.uploads(request).delete(OPERATOR_ID, upload_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="upload not found") from None
    # The upload may also be a chat attachment; drop the now-dangling reference from any
    # message that listed it, so the conversation never points at bytes that are gone.
    await deps.store(request).detach_upload(OPERATOR_ID, upload_id)
