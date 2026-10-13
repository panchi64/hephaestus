"""Skills surface — the operator's library of reusable know-how (`SKILL-1`…`SKILL-3`).

Thin pass-throughs to :class:`~services.skills.store.SkillStore`: list/get, create, full
and surgical edits, publish/unpublish, delete, the bundle's supporting files, and the two
endpoints that make a skill *portable* — import a packaged bundle (or a lone ``SKILL.md``)
and export one back out in the Agent Skills format any other tool reads.

Two things this surface is deliberate about:

* **Publish is a distinct endpoint, not a field on the update body.** It is the boundary
  between "a document the operator is drafting" and "instructions the agent will follow", so
  it takes its own call rather than riding along with a title change.
* **Platform skills are read-only.** They ship with the code; the operator can only switch
  one off, or shadow it with a stored skill of the same name.
* **Validation errors name their field.** ``SkillValidationError`` carries which part of the
  bundle is wrong; that reaches the client as a 422 whose detail the editor renders verbatim,
  because the backend decides what's valid and the frontend only shows it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, File, HTTPException, Request, Response, UploadFile
from pydantic import BaseModel

from core.exceptions import SkillSpanError, SkillValidationError
from routes import deps
from routes.camel import CamelModel
from routes.deps import OPERATOR_ID
from routes.revisions import add_revision_routes
from services.skills import BUNDLE_MAX_BYTES, SkillSummaryView, SkillView, render_skill_md

router = APIRouter(prefix="/skills", tags=["skills"])
add_revision_routes(router, deps.skills)


class SkillCreate(BaseModel):
    name: str
    description: str
    body: str = ""


class SkillUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    body: str | None = None
    license: str | None = None
    compatibility: str | None = None
    metadata: dict[str, Any] | None = None
    allowed_tools: list[str] | None = None


class SkillSpanEdit(BaseModel):
    """A `SKILL-3` targeted edit — replace one exact span rather than rewriting the body."""

    old_text: str
    new_text: str


class SkillFileOut(CamelModel):
    relpath: str
    sha256: str
    size_bytes: int


class SkillSummaryOut(CamelModel):
    """A library-list row — no body, no bundle bytes."""

    id: str
    name: str
    description: str
    published: bool
    pinned: bool
    source: str
    file_count: int
    size_bytes: int
    created_at: datetime
    updated_at: datetime


class SkillOut(CamelModel):
    id: str
    name: str
    description: str
    body: str
    published: bool
    pinned: bool
    source: str
    created_at: datetime
    updated_at: datetime
    license: str | None = None
    compatibility: str | None = None
    metadata: dict[str, Any] | None = None
    #: Advisory: recorded and displayed, never enforced (the tool policy enforces).
    allowed_tools: list[str] | None = None
    #: Non-standard frontmatter preserved from the bundle, so an export is lossless.
    extras: dict[str, Any] | None = None
    files: list[SkillFileOut] = []


class PlatformSkillOut(CamelModel):
    name: str
    description: str
    body: str
    enabled: bool
    #: A published stored skill of the same name is opened instead.
    shadowed: bool
    files: list[str] = []


class SkillImportOut(CamelModel):
    """An import's result: the (draft) skill, plus what the operator should know about it."""

    skill: SkillOut
    warnings: list[str] = []


def _summary_out(view: SkillSummaryView) -> SkillSummaryOut:
    return SkillSummaryOut(
        id=view.id,
        name=view.name,
        description=view.description,
        published=view.published,
        pinned=view.pinned,
        source=view.source,
        file_count=view.file_count,
        size_bytes=view.size_bytes,
        created_at=view.created_at,
        updated_at=view.updated_at,
    )


def _out(view: SkillView) -> SkillOut:
    return SkillOut(
        id=view.id,
        name=view.name,
        description=view.description,
        body=view.body,
        published=view.published,
        pinned=view.pinned,
        source=view.source,
        created_at=view.created_at,
        updated_at=view.updated_at,
        license=view.license,
        compatibility=view.compatibility,
        metadata=view.metadata,
        allowed_tools=view.allowed_tools,
        extras=view.extras,
        files=[
            SkillFileOut(relpath=f.relpath, sha256=f.sha256, size_bytes=f.size_bytes)
            for f in view.files
        ],
    )


def _invalid(exc: SkillValidationError) -> HTTPException:
    return HTTPException(status_code=422, detail={"field": exc.field, "message": str(exc)})


@router.get("", response_model=list[SkillSummaryOut])
async def list_skills(request: Request, published_only: bool = False) -> list[SkillSummaryOut]:
    views = await deps.skills(request).list_skills(OPERATOR_ID, published_only=published_only)
    return [_summary_out(view) for view in views]


@router.get("/platform", response_model=list[PlatformSkillOut])
async def list_platform_skills(request: Request) -> list[PlatformSkillOut]:
    platform = deps.platform_skills(request)
    disabled = await platform.disabled(OPERATOR_ID)
    stored = {entry.name for entry in await deps.skills(request).catalog(OPERATOR_ID)}
    return [
        PlatformSkillOut(
            name=entry.skill.name,
            description=entry.skill.description,
            body=entry.skill.body,
            enabled=entry.skill.name not in disabled,
            shadowed=entry.skill.name in stored,
            files=[relpath for relpath, _ in entry.files],
        )
        for entry in platform.all()
    ]


@router.post("/platform/{name}/enable", status_code=204)
async def enable_platform_skill(name: str, request: Request) -> None:
    await deps.platform_skills(request).set_enabled(OPERATOR_ID, name, True)


@router.post("/platform/{name}/disable", status_code=204)
async def disable_platform_skill(name: str, request: Request) -> None:
    await deps.platform_skills(request).set_enabled(OPERATOR_ID, name, False)


@router.post("", response_model=SkillOut, status_code=201)
async def create_skill(body: SkillCreate, request: Request) -> SkillOut:
    try:
        view = await deps.skills(request).create(
            OPERATOR_ID, name=body.name, description=body.description, body=body.body
        )
    except SkillValidationError as exc:
        raise _invalid(exc) from None
    return _out(view)


@router.post("/import", response_model=SkillImportOut, status_code=201)
async def import_skill(
    request: Request,
    file: UploadFile = File(...),  # noqa: B008 — FastAPI's parameter-marker default
) -> SkillImportOut:
    """Import a `.zip` bundle or a bare `SKILL.md`. The result is always a **draft**: a
    bundle is instructions the agent would follow, so it stays invisible to the model until
    the operator has read it and published it."""
    content = await file.read()
    if not content:
        raise HTTPException(status_code=422, detail="file is empty")
    if len(content) > BUNDLE_MAX_BYTES:
        raise HTTPException(
            status_code=413, detail=f"bundle exceeds the {BUNDLE_MAX_BYTES}-byte limit"
        )
    try:
        view, warnings = await deps.skills(request).import_bundle(
            OPERATOR_ID, content, file.filename or "skill.zip"
        )
    except SkillValidationError as exc:
        raise _invalid(exc) from None
    return SkillImportOut(skill=_out(view), warnings=warnings)


class SkillDocument(BaseModel):
    """A skill's whole ``SKILL.md``: frontmatter and instructions."""

    content: str


@router.get("/{skill_id}/document", response_model=SkillDocument)
async def get_skill_document(skill_id: str, request: Request) -> SkillDocument:
    view = await deps.skills(request).get(OPERATOR_ID, skill_id)
    return SkillDocument(content=render_skill_md(view.parsed()))


@router.put("/{skill_id}/document", response_model=SkillOut)
async def put_skill_document(skill_id: str, body: SkillDocument, request: Request) -> SkillOut:
    """Rewrite the skill from an edited ``SKILL.md``, validated as an import would be."""
    try:
        view = await deps.skills(request).replace_document(OPERATOR_ID, skill_id, body.content)
    except SkillValidationError as exc:
        raise _invalid(exc) from None
    return _out(view)


@router.get("/{skill_id}", response_model=SkillOut)
async def get_skill(skill_id: str, request: Request) -> SkillOut:
    return _out(await deps.skills(request).get(OPERATOR_ID, skill_id))


@router.patch("/{skill_id}", response_model=SkillOut)
async def update_skill(skill_id: str, body: SkillUpdate, request: Request) -> SkillOut:
    try:
        view = await deps.skills(request).update(
            OPERATOR_ID,
            skill_id,
            name=body.name,
            description=body.description,
            body=body.body,
            license=body.license,
            compatibility=body.compatibility,
            metadata=body.metadata,
            allowed_tools=body.allowed_tools,
        )
    except SkillValidationError as exc:
        raise _invalid(exc) from None
    return _out(view)


@router.patch("/{skill_id}/span", response_model=SkillOut)
async def edit_skill_span(skill_id: str, body: SkillSpanEdit, request: Request) -> SkillOut:
    try:
        view = await deps.skills(request).replace_span(
            OPERATOR_ID, skill_id, body.old_text, body.new_text
        )
    except SkillSpanError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return _out(view)


@router.post("/{skill_id}/publish", response_model=SkillOut)
async def publish_skill(skill_id: str, request: Request) -> SkillOut:
    return await _set_published(skill_id, request, True)


@router.post("/{skill_id}/unpublish", response_model=SkillOut)
async def unpublish_skill(skill_id: str, request: Request) -> SkillOut:
    return await _set_published(skill_id, request, False)


async def _set_published(skill_id: str, request: Request, published: bool) -> SkillOut:
    try:
        view = await deps.skills(request).set_published(OPERATOR_ID, skill_id, published)
    except SkillValidationError as exc:
        raise _invalid(exc) from None
    return _out(view)


@router.post("/{skill_id}/pin", response_model=SkillOut)
async def pin_skill(skill_id: str, request: Request) -> SkillOut:
    return _out(await deps.skills(request).set_pinned(OPERATOR_ID, skill_id, True))


@router.post("/{skill_id}/unpin", response_model=SkillOut)
async def unpin_skill(skill_id: str, request: Request) -> SkillOut:
    return _out(await deps.skills(request).set_pinned(OPERATOR_ID, skill_id, False))


@router.delete("/{skill_id}", status_code=204)
async def delete_skill(skill_id: str, request: Request) -> None:
    await deps.skills(request).delete(OPERATOR_ID, skill_id)


@router.get("/{skill_id}/export")
async def export_skill(skill_id: str, request: Request) -> Response:
    """Download the skill as an Agent Skills bundle — the same shape any other tool reads."""
    filename, data = await deps.skills(request).export_bundle(OPERATOR_ID, skill_id)
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.put("/{skill_id}/files/{relpath:path}", response_model=SkillOut)
async def put_skill_file(
    skill_id: str,
    relpath: str,
    request: Request,
    file: UploadFile = File(...),  # noqa: B008 — FastAPI's parameter-marker default
) -> SkillOut:
    content = await file.read()
    try:
        view = await deps.skills(request).put_file(OPERATOR_ID, skill_id, relpath, content)
    except SkillValidationError as exc:
        raise _invalid(exc) from None
    return _out(view)


@router.get("/{skill_id}/files/{relpath:path}")
async def get_skill_file(skill_id: str, relpath: str, request: Request) -> Response:
    content = await deps.skills(request).file_content(OPERATOR_ID, skill_id, relpath)
    return Response(
        content=content,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{relpath.rsplit("/", 1)[-1]}"'},
    )


@router.delete("/{skill_id}/files/{relpath:path}", response_model=SkillOut)
async def delete_skill_file(skill_id: str, relpath: str, request: Request) -> SkillOut:
    return _out(await deps.skills(request).delete_file(OPERATOR_ID, skill_id, relpath))
