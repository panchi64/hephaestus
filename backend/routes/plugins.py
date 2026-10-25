"""Plugins surface — reviewing, publishing and configuring the plugins agents write.

Publishing is the operator's act: it is what turns a draft's code into tools the model
can call, so it has its own endpoint, and no agent tool reaches it. Secret values are
written here and never read back.
"""

from __future__ import annotations

import io
import zipfile
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, File, HTTPException, Request, Response, UploadFile
from pydantic import BaseModel

from routes import deps
from routes.deps import OPERATOR_ID
from routes.revisions import add_revision_routes
from services.plugins import PluginManifest, PluginView
from services.plugins.contributions.base import Described
from services.plugins.manifest import BUNDLE_MAX_BYTES, validate_path

router = APIRouter(prefix="/plugins", tags=["plugins"])
add_revision_routes(router, deps.plugins)


class ToolOut(BaseModel):
    name: str
    description: str
    sensitivity: str
    parameters: dict[str, object]


class ManifestOut(BaseModel):
    description: str
    requirements: list[str]
    egress: list[str]
    #: Secret name → the host its value is for.
    secrets: dict[str, str]
    tools: list[ToolOut]
    #: Names of the agents, skills and routine templates it contributes.
    agents: list[str]
    skills: list[str]
    routines: list[str] = []
    #: Every contribution by type key, as each type describes its items.
    contributions: dict[str, list[Described]] = {}


class PluginOut(BaseModel):
    id: str
    name: str
    source: Literal["user", "agent"]
    published: bool
    #: The version its tools run; null until first published.
    active: ManifestOut | None
    draft: ManifestOut | None
    created_at: datetime
    updated_at: datetime


class FileOut(BaseModel):
    path: str
    #: Null for a file that is not UTF-8 text.
    content: str | None
    size_bytes: int


class SecretOut(BaseModel):
    name: str
    domain: str
    is_set: bool


class SecretWrite(BaseModel):
    value: str
    #: The host the value may be sent to; the manifest's when omitted.
    domain: str | None = None


@router.get("", response_model=list[PluginOut])
async def list_plugins(request: Request) -> list[PluginOut]:
    return [_out(view) for view in await deps.plugins(request).list(OPERATOR_ID)]


@router.get("/{plugin_id}", response_model=PluginOut)
async def get_plugin(request: Request, plugin_id: str) -> PluginOut:
    return _out(await deps.plugins(request).get(OPERATOR_ID, plugin_id))


@router.get("/{plugin_id}/files", response_model=list[FileOut])
async def get_files(
    request: Request, plugin_id: str, version: Literal["current", "draft", "active"] = "current"
) -> list[FileOut]:
    files = await deps.plugins(request).files(OPERATOR_ID, plugin_id, version)
    return [
        FileOut(path=path, content=_text(blob), size_bytes=len(blob))
        for path, blob in sorted(files.items())
    ]


@router.post("/import", response_model=PluginOut, status_code=201)
async def import_plugin(request: Request, file: Annotated[UploadFile, File()]) -> PluginOut:
    """A plugin from a zip of its files, as a draft."""
    data = await file.read(BUNDLE_MAX_BYTES * 4 + 1)
    if len(data) > BUNDLE_MAX_BYTES * 4:
        raise HTTPException(status_code=413, detail="that archive is too large")
    files = _unzip(data)
    return _out(await deps.plugins(request).import_files(OPERATOR_ID, files))


@router.get("/{plugin_id}/export")
async def export_plugin(
    request: Request, plugin_id: str, version: Literal["current", "active"] = "current"
) -> Response:
    store = deps.plugins(request)
    view = await store.get(OPERATOR_ID, plugin_id)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, blob in sorted((await store.files(OPERATOR_ID, plugin_id, version)).items()):
            archive.writestr(f"{view.name}/{path}", blob)
    return Response(
        content=buffer.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{view.name}.zip"'},
    )


@router.post("/{plugin_id}/publish", response_model=PluginOut)
async def publish_plugin(request: Request, plugin_id: str) -> PluginOut:
    """Publish the draft, or switch a disabled plugin back on."""
    return _out(await deps.plugins(request).publish(OPERATOR_ID, plugin_id))


@router.post("/{plugin_id}/unpublish", response_model=PluginOut)
async def unpublish_plugin(request: Request, plugin_id: str) -> PluginOut:
    return _out(await deps.plugins(request).unpublish(OPERATOR_ID, plugin_id))


@router.delete("/{plugin_id}/draft")
async def discard_draft(request: Request, plugin_id: str) -> Response:
    """Drop the draft. A plugin that was never published is deleted with it (204)."""
    view = await deps.plugins(request).discard_draft(OPERATOR_ID, plugin_id)
    if view is None:
        return Response(status_code=204)
    return Response(content=_out(view).model_dump_json(), media_type="application/json")


@router.delete("/{plugin_id}", status_code=204)
async def delete_plugin(request: Request, plugin_id: str) -> Response:
    await deps.plugins(request).delete(OPERATOR_ID, plugin_id)
    return Response(status_code=204)


@router.get("/{plugin_id}/secrets", response_model=list[SecretOut])
async def list_secrets(request: Request, plugin_id: str) -> list[SecretOut]:
    statuses = await deps.plugins(request).secrets(OPERATOR_ID, plugin_id)
    return [SecretOut(name=s.name, domain=s.domain, is_set=s.is_set) for s in statuses]


@router.put("/{plugin_id}/secrets/{name}", status_code=204)
async def set_secret(request: Request, plugin_id: str, name: str, body: SecretWrite) -> Response:
    await deps.plugins(request).set_secret(OPERATOR_ID, plugin_id, name, body.value, body.domain)
    return Response(status_code=204)


@router.delete("/{plugin_id}/secrets/{name}", status_code=204)
async def delete_secret(request: Request, plugin_id: str, name: str) -> Response:
    await deps.plugins(request).delete_secret(OPERATOR_ID, plugin_id, name)
    return Response(status_code=204)


def _out(view: PluginView) -> PluginOut:
    return PluginOut(
        id=view.id,
        name=view.name,
        source=view.source,
        published=view.published,
        active=None if view.active is None else _manifest(view.active),
        draft=None if view.draft is None else _manifest(view.draft),
        created_at=view.created_at,
        updated_at=view.updated_at,
    )


def _manifest(manifest: PluginManifest) -> ManifestOut:
    described = manifest.described()
    return ManifestOut(
        description=manifest.description,
        requirements=list(manifest.requirements),
        egress=list(manifest.egress),
        secrets=dict(manifest.secrets),
        tools=[
            ToolOut(
                name=tool.name,
                description=tool.description,
                sensitivity=tool.sensitivity.value,
                parameters=tool.parameters,
            )
            for tool in manifest.tools
        ],
        agents=[agent.name for agent in manifest.agents],
        skills=[entry.skill.name for entry in manifest.skills],
        routines=[str(item["name"]) for item in described.get("routines", [])],
        contributions=described,
    )


def _text(blob: bytes) -> str | None:
    try:
        return blob.decode()
    except UnicodeDecodeError:
        return None


def _unzip(data: bytes) -> dict[str, bytes]:
    """The archive's files, with one shared top folder stripped."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise HTTPException(status_code=422, detail="that is not a zip archive") from None
    entries = [info for info in archive.infolist() if not info.is_dir()]
    if sum(info.file_size for info in entries) > BUNDLE_MAX_BYTES:
        raise HTTPException(status_code=413, detail="a plugin is at most 1 MB of files")
    names = [info.filename for info in entries]
    tops = {name.split("/", 1)[0] for name in names}
    strip = len(tops) == 1 and all("/" in name for name in names)
    files: dict[str, bytes] = {}
    for info in entries:
        path = info.filename.split("/", 1)[1] if strip else info.filename
        files[validate_path(path)] = archive.read(info)
    return files
