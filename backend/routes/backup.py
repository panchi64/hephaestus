"""Backup & restore (`BACKUP-1..2`).

See ``routes/mail.py`` for why the surface is registered before it exists.

The export comes back as the envelope **JSON object** rather than a file download: the whole
format is already a single JSON document, and returning it as a normal response lets the
browser client save it with the bytes it has in hand — no second auth-gated content endpoint,
no download token. Import takes the same object back.

A wrong backup secret answers **400**, never 401/423: those two codes mean *the session*
failed app-wide (the frontend client clears the token on either), and mistyping a recovery
passphrase is not that.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from core.vault import VaultLocked
from routes import deps
from routes.camel import CamelModel
from routes.deps import OPERATOR_ID
from services.backup import (
    BackupFormatError,
    BackupManifest,
    BackupPayloadError,
    BackupSecretError,
)

router = APIRouter(prefix="/backup", tags=["backup"])


class ManifestItemOut(BaseModel):
    name: str
    count: int


class ManifestOut(CamelModel):
    created_at: datetime
    items: list[ManifestItemOut]


class ContentsOut(BaseModel):
    """What an export would contain if taken right now — the groups that actually exist
    (discovered from the models, never a hardcoded menu) and their current counts."""

    sections: list[str]
    items: list[ManifestItemOut]


class ExportIn(BaseModel):
    # The operator's recovery secret. Not stored anywhere: lose it and the file is
    # unrecoverable, which is the point of it being separate from the login password.
    secret: str = Field(min_length=1)
    include: list[str] | None = None


class ImportIn(BaseModel):
    secret: str = Field(min_length=1)
    envelope: dict[str, Any]
    include: list[str] | None = None


class ExportOut(BaseModel):
    envelope: dict[str, Any]
    manifest: ManifestOut


class ImportOut(BaseModel):
    imported: dict[str, int]
    skipped: dict[str, int]
    # Groups the file carried that this build has no table for — a backup from a newer
    # version. Reported rather than swallowed, so a partial restore is never silent.
    unknown: list[str]


def _manifest_out(manifest: BackupManifest) -> ManifestOut:
    return ManifestOut(
        created_at=manifest.created_at,
        items=[ManifestItemOut(name=i.name, count=i.count) for i in manifest.items],
    )


@router.get("/manifest", response_model=ManifestOut | None)
async def last_backup(request: Request) -> ManifestOut | None:
    """The last export the operator took, or null if they never have. Absent, not
    invented — the screen renders its own empty state from that."""
    manifest = await deps.backup(request).last_manifest(OPERATOR_ID)
    return _manifest_out(manifest) if manifest else None


@router.get("/contents", response_model=ContentsOut)
async def backup_contents(request: Request) -> ContentsOut:
    service = deps.backup(request)
    try:
        items = await service.counts(OPERATOR_ID)
    except VaultLocked:
        raise HTTPException(status_code=409, detail="the app is locked") from None
    return ContentsOut(
        sections=list(service.sections()),
        items=[ManifestItemOut(name=i.name, count=i.count) for i in items],
    )


@router.post("/export", response_model=ExportOut)
async def export_backup(body: ExportIn, request: Request) -> ExportOut:
    try:
        envelope, manifest = await deps.backup(request).export(
            OPERATOR_ID, body.secret, include=body.include
        )
    except VaultLocked:
        # Nothing can be read out to export while the app itself is locked.
        raise HTTPException(status_code=409, detail="the app is locked") from None
    return ExportOut(envelope=envelope, manifest=_manifest_out(manifest))


@router.post("/import", response_model=ImportOut)
async def import_backup(body: ImportIn, request: Request) -> ImportOut:
    try:
        report = await deps.backup(request).import_backup(
            OPERATOR_ID, body.secret, body.envelope, include=body.include
        )
    except BackupSecretError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except (BackupFormatError, BackupPayloadError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except VaultLocked:
        raise HTTPException(status_code=409, detail="the app is locked") from None
    return ImportOut(imported=report.imported, skipped=report.skipped, unknown=list(report.unknown))
