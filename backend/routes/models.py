"""Model registry surface — manage endpoints and role→chain bindings.

The operator's catalog of model endpoints and the role bindings that map ``main``
/ ``utility`` / ``embedding`` to ordered fallback chains. The API key is
**write-only**: it is accepted on create/update and sealed with the vault, but
never returned — listings expose only ``has_api_key``.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from core.exceptions import DegradedCapabilityError, NotFoundError
from models.registry import ModelEndpoint
from routes import deps
from routes.deps import OPERATOR_ID
from services import llm
from services.providers import DEFAULT_PROVIDER_ID, all_providers
from services.reindex import ReindexStatus

router = APIRouter(prefix="/models", tags=["models"])


class EndpointCreate(BaseModel):
    name: str
    base_url: str
    provider: str = DEFAULT_PROVIDER_ID
    model: str | None = None
    api_key: str | None = None
    context_window: int | None = None
    native_tools: bool = True
    vision: bool = False
    thinking: bool = False
    enabled: bool = True
    max_concurrent_runs: int | None = Field(default=None, ge=1)


class EndpointUpdate(BaseModel):
    name: str | None = None
    base_url: str | None = None
    provider: str | None = None
    model: str | None = None
    api_key: str | None = None  # "" clears the key; omitted leaves it unchanged
    context_window: int | None = None
    native_tools: bool | None = None
    vision: bool | None = None
    thinking: bool | None = None
    enabled: bool | None = None
    max_concurrent_runs: int | None = Field(default=None, ge=0)  # 0 clears it


class EndpointView(BaseModel):
    id: str
    name: str
    provider: str
    base_url: str
    model: str | None
    has_api_key: bool
    # The operator's *override*, null on every endpoint that never needed one. It is not
    # the window the endpoint runs under and no read surface should treat it as one — a
    # window belongs to the (endpoint, model) pair, and the model is the role's. See
    # `RoleView.context_window`.
    context_window: int | None
    native_tools: bool
    vision: bool
    thinking: bool
    # None ⇒ the server's reported slot count, or unlimited.
    max_concurrent_runs: int | None
    # Disable-without-delete + last connection-test health. ``last_*`` carry the
    # backend's verdict verbatim so the catalog list shows at-a-glance status without
    # a probe per row; they are null until the endpoint has been tested.
    enabled: bool
    last_status: str | None
    last_error_category: str | None
    last_error_detail: str | None
    last_checked_at: datetime | None


def _view(endpoint: ModelEndpoint) -> EndpointView:
    return EndpointView(
        id=endpoint.id,
        name=endpoint.name,
        provider=endpoint.provider,
        base_url=endpoint.base_url,
        model=endpoint.model,
        has_api_key=endpoint.api_key_enc is not None,
        context_window=endpoint.context_window,
        native_tools=endpoint.native_tools,
        vision=endpoint.vision,
        thinking=endpoint.thinking,
        max_concurrent_runs=endpoint.max_concurrent_runs,
        enabled=endpoint.enabled,
        last_status=endpoint.last_status,
        last_error_category=endpoint.last_error_category,
        last_error_detail=endpoint.last_error_detail,
        last_checked_at=endpoint.last_checked_at,
    )


class ProviderView(BaseModel):
    """One provider adapter, as the endpoint editor offers it — the preset is what
    the form prefills, so the frontend never hardcodes a lab's details."""

    id: str
    display_name: str
    requires_key: bool
    default_base_url: str | None
    key_hint: str | None
    docs_url: str | None
    native_tools: bool
    vision: bool


@router.get("/providers", response_model=list[ProviderView])
async def list_providers() -> list[ProviderView]:
    return [
        ProviderView(
            id=p.id,
            display_name=p.display_name,
            requires_key=p.requires_key,
            default_base_url=p.preset.default_base_url,
            key_hint=p.preset.key_hint,
            docs_url=p.preset.docs_url,
            native_tools=p.preset.native_tools,
            vision=p.preset.vision,
        )
        for p in all_providers()
    ]


@router.get("/endpoints", response_model=list[EndpointView])
async def list_endpoints(request: Request) -> list[EndpointView]:
    endpoints = await deps.models(request).list_endpoints(OPERATOR_ID)
    return [_view(e) for e in endpoints]


@router.post("/endpoints", status_code=201, response_model=EndpointView)
async def create_endpoint(body: EndpointCreate, request: Request) -> EndpointView:
    try:
        endpoint = await deps.models(request).create_endpoint(
            OPERATOR_ID,
            name=body.name,
            base_url=body.base_url,
            provider=body.provider,
            model=body.model,
            api_key=body.api_key,
            context_window=body.context_window,
            native_tools=body.native_tools,
            vision=body.vision,
            thinking=body.thinking,
            enabled=body.enabled,
            max_concurrent_runs=body.max_concurrent_runs,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return _view(endpoint)


@router.get("/endpoints/{endpoint_id}", response_model=EndpointView)
async def get_endpoint(endpoint_id: str, request: Request) -> EndpointView:
    try:
        endpoint = await deps.models(request).get_endpoint(OPERATOR_ID, endpoint_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="endpoint not found") from None
    return _view(endpoint)


class EndpointModels(BaseModel):
    models: list[str]
    # False when the provider has no models API (or is unreachable) — the picker
    # then falls back to the endpoint's configured model instead of a live list.
    supported: bool


@router.get("/endpoints/{endpoint_id}/models", response_model=EndpointModels)
async def list_endpoint_models(endpoint_id: str, request: Request) -> EndpointModels:
    try:
        models = await deps.models(request).list_provider_models(OPERATOR_ID, endpoint_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="endpoint not found") from None
    except DegradedCapabilityError:
        return EndpointModels(models=[], supported=False)
    return EndpointModels(models=models, supported=True)


class EndpointHealth(BaseModel):
    """A connection test's verdict — backend-categorized, rendered verbatim by the UI."""

    status: str  # "ok" | "error"
    error_category: str  # ok|auth|rate_limited|timeout|unreachable|bad_response|server_error
    error_detail: str
    checked_at: datetime


@router.post("/endpoints/{endpoint_id}/test", response_model=EndpointHealth)
async def check_endpoint(endpoint_id: str, request: Request) -> EndpointHealth:
    """Probe the endpoint now and return its health. The category and the plain-language
    detail are decided by the backend — the frontend never maps HTTP codes to messages."""
    try:
        health = await deps.models(request).test_endpoint(OPERATOR_ID, endpoint_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="endpoint not found") from None
    return EndpointHealth(
        status=health.status,
        error_category=health.error_category,
        error_detail=health.error_detail,
        checked_at=health.checked_at,
    )


@router.patch("/endpoints/{endpoint_id}", response_model=EndpointView)
async def update_endpoint(endpoint_id: str, body: EndpointUpdate, request: Request) -> EndpointView:
    changes: dict[str, object] = body.model_dump(exclude_unset=True)
    try:
        endpoint = await deps.models(request).update_endpoint(OPERATOR_ID, endpoint_id, **changes)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="endpoint not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return _view(endpoint)


@router.delete("/endpoints/{endpoint_id}", status_code=204)
async def delete_endpoint(endpoint_id: str, request: Request) -> None:
    try:
        await deps.models(request).delete_endpoint(OPERATOR_ID, endpoint_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="endpoint not found") from None


class RoleBinding(BaseModel):
    endpoint_ids: list[str]
    # An explicit model on the bound endpoint — used by ``embedding`` (no
    # per-conversation picker, unlike ``main``); ``None`` ⇒ the endpoint's default.
    model: str | None = None


class RoleView(BaseModel):
    endpoint_ids: list[str]
    model: str | None = None
    # The chain head's effective context window: the operator's override on the endpoint
    # when set, else what the provider reports for the pinned model. Null when the role
    # is unconfigured or neither could supply one — which for `main` is the state the
    # backend refuses to send a turn in, so it is also what the composer gates on.
    #
    # It lives here rather than on the endpoint because that is where the *model* is
    # decided. An endpoint row carries only a default model and usually doesn't set one;
    # reading a window off it answers null on exactly this workspace's shape — one
    # server, many models, the choice made in the picker.
    context_window: int | None = None
    # True when this is the backend's default rather than the operator's choice: `main`
    # with nothing bound resolves to the first usable endpoint/model instead of
    # degrading, and reports what it resolved to here. A surface should say "defaulting
    # to this" rather than showing it as pinned — the default moves when a better
    # endpoint appears, which a pin would not.
    implicit: bool = False
    # True on **`main`** when background work — a permission review, a title, a summary, a
    # sub-agent — will run on the same server the operator chats on, because `utility` is
    # unbound or is bound to the same `base_url`. Additive, same posture as `implicit`
    # above: a fact a surface may state, never one it derives.
    #
    # On `main` rather than on `utility`, which is where the plan for this put it, for a
    # mechanical reason: `list_roles` returns *stored* bindings, so the commonest shape —
    # and the one where this is most true — has no `utility` row at all to carry a flag.
    # `main` always has a row, implicit or bound, and it is `main`'s prompt cache that the
    # background call evicts, so the flag is about this row's endpoint either way.
    background_shares_endpoint: bool = False


@router.get("/roles", response_model=dict[str, RoleView])
async def list_roles(request: Request) -> dict[str, RoleView]:
    models = deps.models(request)
    chains = await models.list_roles(OPERATOR_ID)
    pinned = await models.list_role_models(OPERATOR_ID)
    out = {
        role: RoleView(
            endpoint_ids=ids,
            model=pinned.get(role),
            # Memoized per (base_url, model) in the registry, so the first listing after
            # a change pays for discovery and the rest are free. Bounded either way: the
            # lookup has its own short timeouts and never raises.
            context_window=await models.role_context_window(OPERATOR_ID, role),
        )
        for role, ids in chains.items()
    }
    # `list_roles` returns only *stored* bindings, so an unbound `main` is absent —
    # and the picker, which reads this, would have nothing to show for the model a
    # turn would actually run on. Report the implicit resolution under the same key,
    # flagged, so display and execution cannot disagree.
    if not out.get("main") or not out["main"].endpoint_ids:
        implicit = await models.implicit_main_binding(OPERATOR_ID)
        if implicit is not None:
            chain_ids, model = implicit
            out["main"] = RoleView(
                endpoint_ids=chain_ids,
                model=model,
                context_window=await models.role_context_window(OPERATOR_ID, "main"),
                implicit=True,
            )
    # Stamped after the implicit fill-in, so it lands whether `main` was bound or defaulted.
    if "main" in out:
        out["main"] = out["main"].model_copy(
            update={"background_shares_endpoint": await models.background_shares_main(OPERATOR_ID)}
        )
    return out


@router.put("/roles/{role}", status_code=204)
async def set_role(role: str, body: RoleBinding, request: Request) -> None:
    if role not in llm.ROLES:
        raise HTTPException(status_code=422, detail=f"unknown role {role!r}")
    models = deps.models(request)
    prev = None
    if role == "embedding":
        prev = await models.get_role_binding(OPERATOR_ID, role)
    try:
        await models.set_role(OPERATOR_ID, role, body.endpoint_ids, model=body.model)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="endpoint not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # A changed embedding endpoint/model strands existing vectors (EMB-2 segregates
    # by model) — heal them in the background so semantic recall recovers without
    # any further operator action.
    if role == "embedding" and prev != (body.endpoint_ids, body.model):
        deps.embedding_reindexer(request).trigger(OPERATOR_ID)


class ReindexStatusView(BaseModel):
    state: str  # idle | running | done | degraded | error
    memories: int
    messages: int
    detail: str | None = None
    completed_at: datetime | None = None


def _reindex_view(status: ReindexStatus) -> ReindexStatusView:
    return ReindexStatusView(
        state=status.state,
        memories=status.memories,
        messages=status.messages,
        detail=status.detail,
        completed_at=status.completed_at,
    )


@router.post("/embedding/reindex", response_model=ReindexStatusView)
async def trigger_embedding_reindex(request: Request) -> ReindexStatusView:
    """Manually re-embed memories + the chat index against the current embedding
    model — for a first index that failed, or to force a redo after a model change."""
    reindexer = deps.embedding_reindexer(request)
    reindexer.trigger(OPERATOR_ID)
    return _reindex_view(reindexer.status())


@router.get("/embedding/reindex", response_model=ReindexStatusView)
async def embedding_reindex_status(request: Request) -> ReindexStatusView:
    return _reindex_view(deps.embedding_reindexer(request).status())
