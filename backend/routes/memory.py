"""Memory surface — store, browse, recall, and audit long-term memories.

Thin pass-throughs to the memory capability (`MEM-*`). The same store the agent's
memory tools call, so direct operator management and agent recall share one
implementation. Content is returned decrypted — the operator owns it.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from core.exceptions import NotFoundError
from routes import deps
from routes.deps import OPERATOR_ID
from services.memory import DuplicateGroup, MemoryView, RecallHit

router = APIRouter(prefix="/memory", tags=["memory"])


class MemoryCreate(BaseModel):
    content: str
    # Blank reads as the content's first line.
    description: str | None = None
    pinned: bool = False


class MemoryUpdate(BaseModel):
    content: str | None = None
    description: str | None = None
    pinned: bool | None = None


class MemoryOut(BaseModel):
    id: str
    content: str
    description: str
    pinned: bool
    created_at: datetime
    updated_at: datetime
    has_embedding: bool


class RecallRequest(BaseModel):
    query: str
    limit: int = 5


class RecallHitOut(BaseModel):
    content: str
    matched_by: str
    score: float
    id: str


class DuplicateGroupOut(BaseModel):
    memory_ids: list[str]
    similarity: float


def _out(view: MemoryView) -> MemoryOut:
    return MemoryOut(
        id=view.id,
        content=view.content,
        description=view.description,
        pinned=view.pinned,
        created_at=view.created_at,
        updated_at=view.updated_at,
        has_embedding=view.has_embedding,
    )


@router.get("", response_model=list[MemoryOut])
async def list_memories(request: Request) -> list[MemoryOut]:
    views = await deps.memory(request).list_memories(OPERATOR_ID)
    return [_out(v) for v in views]


@router.post("", status_code=201, response_model=MemoryOut)
async def create_memory(body: MemoryCreate, request: Request) -> MemoryOut:
    if not body.content.strip():
        raise HTTPException(status_code=422, detail="content must not be empty")
    view = await deps.memory(request).remember(
        OPERATOR_ID, body.content, description=body.description, pinned=body.pinned
    )
    return _out(view)


@router.post("/recall", response_model=list[RecallHitOut])
async def recall_memories(body: RecallRequest, request: Request) -> list[RecallHitOut]:
    hits: list[RecallHit] = await deps.memory(request).recall(
        OPERATOR_ID, body.query, limit=body.limit
    )
    return [
        RecallHitOut(
            content=h.memory.content, matched_by=h.matched_by, score=h.score, id=h.memory.id
        )
        for h in hits
    ]


@router.post("/audit", response_model=list[DuplicateGroupOut])
async def audit_memories(request: Request) -> list[DuplicateGroupOut]:
    groups: list[DuplicateGroup] = await deps.memory(request).audit(OPERATOR_ID)
    return [DuplicateGroupOut(memory_ids=g.memory_ids, similarity=g.similarity) for g in groups]


@router.get("/{memory_id}", response_model=MemoryOut)
async def get_memory(memory_id: str, request: Request) -> MemoryOut:
    try:
        view = await deps.memory(request).get(OPERATOR_ID, memory_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="memory not found") from None
    return _out(view)


@router.patch("/{memory_id}", response_model=MemoryOut)
async def update_memory(memory_id: str, body: MemoryUpdate, request: Request) -> MemoryOut:
    try:
        view = await deps.memory(request).update(
            OPERATOR_ID,
            memory_id,
            content=body.content,
            description=body.description,
            pinned=body.pinned,
        )
    except NotFoundError:
        raise HTTPException(status_code=404, detail="memory not found") from None
    return _out(view)


@router.delete("/{memory_id}", status_code=204)
async def delete_memory(memory_id: str, request: Request) -> None:
    try:
        await deps.memory(request).delete(OPERATOR_ID, memory_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="memory not found") from None
