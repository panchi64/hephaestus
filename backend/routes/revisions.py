"""Version history routes, mounted on each surface whose store keeps history.

``/{id}/revisions`` lists versions newest first; ``/{id}/revisions/changes`` is how the
item now differs from its latest version (what publishing would change);
``/{id}/revisions/{n}`` is how it differs from version ``n``; and restoring version ``n``
rewrites the item as it was, which a published item records as a new version.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

from routes.deps import OPERATOR_ID
from services.revisions import FileDiff, Versioned, compare, restore


class RevisionOut(BaseModel):
    number: int
    note: str
    size_bytes: int
    created_at: datetime


class FileDiffOut(BaseModel):
    path: str
    status: Literal["added", "removed", "changed"]
    patch: str | None


def add_revision_routes(router: APIRouter, store: Callable[[Request], Versioned]) -> None:
    @router.get("/{subject_id}/revisions", response_model=list[RevisionOut])
    async def list_revisions(request: Request, subject_id: str) -> list[RevisionOut]:
        versioned = store(request)
        await versioned.snapshot(OPERATOR_ID, subject_id)
        if versioned.revisions is None:
            return []
        history = await versioned.revisions.history(
            OPERATOR_ID, versioned.revision_kind, subject_id
        )
        return [RevisionOut.model_validate(info, from_attributes=True) for info in history]

    @router.get("/{subject_id}/revisions/changes", response_model=list[FileDiffOut])
    async def unpublished_changes(request: Request, subject_id: str) -> list[FileDiffOut]:
        return _out(await compare(store(request), OPERATOR_ID, subject_id))

    @router.get("/{subject_id}/revisions/{number}", response_model=list[FileDiffOut])
    async def changes_since(request: Request, subject_id: str, number: int) -> list[FileDiffOut]:
        return _out(await compare(store(request), OPERATOR_ID, subject_id, number))

    @router.post("/{subject_id}/revisions/{number}/restore", status_code=204)
    async def restore_revision(request: Request, subject_id: str, number: int) -> Response:
        await restore(store(request), OPERATOR_ID, subject_id, number)
        return Response(status_code=204)


def _out(changes: list[FileDiff]) -> list[FileDiffOut]:
    return [FileDiffOut(path=c.path, status=c.status, patch=c.patch) for c in changes]
