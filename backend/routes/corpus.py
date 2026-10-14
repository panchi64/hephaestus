"""Knowledge corpus surface — the unified retrieval index behind the ``/rag`` screen.

Thin pass-throughs to :class:`~services.corpus.CorpusIndex`: list the sources feeding
the corpus, report index stats, and manage operator-added host folders (add / remove /
reindex / rebuild). Out-shapes are camelCase to match the frontend's ``RagSource`` /
``RagIndexStats`` contracts exactly (the screen swaps mocks for these without changing
its types). The icon a row carries is presentation policy resolved here (surfaces echo
their nav glyph; folders get an archive glyph), not stored on the source.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from core.exceptions import NotFoundError
from routes import deps
from routes.camel import CamelModel
from routes.deps import OPERATOR_ID
from services.corpus import CorpusStats, SourceStatus

router = APIRouter(prefix="/corpus", tags=["corpus"])

# A surface's row glyph by its fixed source id (folders fall back to an archive glyph).
_SURFACE_ICONS = {
    "surf-uploads": "upload",
    "surf-memory": "database",
    "surf-conversations": "chat",
}


class CorpusSourceOut(CamelModel):
    id: str
    kind: str
    label: str
    icon: str
    href: str | None = None
    doc_count: int
    status: str
    last_indexed_at: datetime | None = None
    error_hint: str | None = None


class CorpusStatsOut(CamelModel):
    embedding_model: str | None
    dims: int | None
    total_docs: int
    total_collections: int
    store_size: str


class FolderCreate(BaseModel):
    path: str


def _icon_for(status: SourceStatus) -> str:
    return _SURFACE_ICONS.get(status.source_id, "archive")


def _source_out(status: SourceStatus) -> CorpusSourceOut:
    return CorpusSourceOut(
        id=status.source_id,
        kind=status.kind,
        label=status.label,
        icon=_icon_for(status),
        href=status.href,
        doc_count=status.doc_count,
        status=status.status,
        last_indexed_at=status.last_indexed_at,
        error_hint=status.error_hint,
    )


def _stats_out(stats: CorpusStats) -> CorpusStatsOut:
    return CorpusStatsOut(
        embedding_model=stats.embedding_model,
        dims=stats.dims,
        total_docs=stats.total_docs,
        total_collections=stats.total_collections,
        # No on-disk size accounting yet; the screen renders a placeholder.
        store_size="—",
    )


@router.get("/sources", response_model=list[CorpusSourceOut])
async def list_sources(request: Request) -> list[CorpusSourceOut]:
    sources = await deps.corpus(request).list_sources(OPERATOR_ID)
    return [_source_out(s) for s in sources]


@router.get("/stats", response_model=CorpusStatsOut)
async def corpus_stats(request: Request) -> CorpusStatsOut:
    return _stats_out(await deps.corpus(request).stats(OPERATOR_ID))


@router.post("/folders", status_code=201, response_model=CorpusSourceOut)
async def add_folder(body: FolderCreate, request: Request) -> CorpusSourceOut:
    if not body.path.strip():
        raise HTTPException(status_code=422, detail="path must not be empty")
    corpus = deps.corpus(request)
    path = body.path.strip()
    source = await corpus.add_folder(
        OPERATOR_ID, path, project_id=await deps.active_project(request)
    )
    # Reflect the just-created source as the same row shape the list returns. The label is
    # the path as submitted — the stored column is sealed, and this is the same string.
    return CorpusSourceOut(
        id=source.id,
        kind="folder",
        label=path,
        icon="archive",
        href=None,
        doc_count=0,
        status=source.status,
        last_indexed_at=source.last_indexed_at,
        error_hint=source.error_hint,
    )


@router.delete("/folders/{source_id}", status_code=204)
async def remove_folder(source_id: str, request: Request) -> None:
    if not await deps.corpus(request).remove_folder(OPERATOR_ID, source_id):
        raise HTTPException(status_code=404, detail="source not found")


@router.post("/sources/{source_id}/reindex", status_code=202)
async def reindex_source(source_id: str, request: Request) -> None:
    try:
        _ = await deps.corpus(request).reindex(OPERATOR_ID, source_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="source not found") from None


@router.post("/sources/{source_id}/rebuild", status_code=202)
async def rebuild_source(source_id: str, request: Request) -> None:
    if not await deps.corpus(request).rebuild(OPERATOR_ID, source_id):
        raise HTTPException(status_code=404, detail="source not found")
