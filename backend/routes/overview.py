"""Home overview — a read-only aggregate of real chassis status.

The home page is presentation-only; this endpoint is the single source of truth
it renders. It reports what the backend actually knows: the build version, which
model roles are configured, whether the execution sandbox is present, and counts
of the operator's conversations, memories, and configured endpoints — plus the
capability health derived from those facts (the policy lives here, not in the
frontend). Telemetry and external services that don't exist yet are simply
absent rather than fabricated; they grow rows here as their capabilities land.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from pydantic import BaseModel

from core.config import get_settings
from routes import deps
from routes.deps import OPERATOR_ID

router = APIRouter(prefix="/overview", tags=["overview"])


class Capability(BaseModel):
    """A capability the workspace exposes, with backend-decided health.

    ``status``/``detail`` are rendered verbatim — the frontend does not decide
    what counts as nominal/degraded/down. ``critical`` marks a capability the
    workspace cannot function without (drives the overall-status flag)."""

    key: str  # stable id: "main_model" | "embeddings" | "sandbox" | "email" | "push"
    label: str
    status: str  # "nominal" | "warn" | "alert"
    detail: str
    critical: bool = False
    remediation_href: str | None = None
    remediation_label: str | None = None


class Overview(BaseModel):
    version: str
    endpoint_count: int
    conversation_count: int
    memory_count: int
    active_run_count: int
    capabilities: list[Capability]


@router.get("", response_model=Overview)
async def get_overview(request: Request) -> Overview:
    models = deps.models(request)
    # These five reads are independent — each hits the database on its own threadpool
    # hop — so they are gathered rather than awaited in sequence. This is the home
    # screen's one request; serially it cost the sum of five round trips to render a
    # page that has nothing else to wait for.
    roles, endpoints, search_providers, conversation_count, memory_count = await asyncio.gather(
        models.list_roles(OPERATOR_ID),
        models.list_endpoints(OPERATOR_ID),
        deps.search(request).list_providers(OPERATOR_ID),
        deps.store(request).count_conversations(OPERATOR_ID),
        deps.memory(request).count(OPERATOR_ID),
    )

    # The chat (`main`) model is chosen live from the top-bar picker, not bound
    # here — so the precondition the workspace can't function without is simply
    # that a usable chat endpoint exists. `main` requires native tool-calling
    # (enforced at resolve), so only such endpoints count.
    usable_chat_endpoints = [e for e in endpoints if e.native_tools and e.enabled]
    embedding_configured = bool(roles.get("embedding"))
    sandbox_present = deps.sandbox_sessions(request) is not None
    vm = getattr(request.app.state, "vm", None)
    vm_ready = vm is not None and vm.available
    provider_enabled = any(p.enabled for p in search_providers)
    managed_search_ready = deps.searxng(request).base_url is not None
    web_search_configured = provider_enabled or managed_search_ready
    # Offline mode suspends both web capabilities (their VM services are stopped to save
    # resources); when active it's the reason the rows are down, so it overrides the
    # generic VM detail below.
    offline_active = deps.offline(request).state().effective_offline

    active_runs = [r for r in deps.registry(request).list(OPERATOR_ID) if not r.is_terminal]

    capabilities: list[Capability] = []
    # Chat model — the one capability the workspace can't function without. The
    # operator chooses the live model from the top-bar picker; what the backend
    # asserts here is the precondition for that to be possible at all: at least
    # one native-tool-calling endpoint to chat against.
    if usable_chat_endpoints:
        capabilities.append(
            Capability(
                key="chat_model",
                label="CHAT MODEL",
                status="nominal",
                detail=f"{len(usable_chat_endpoints)} endpoint"
                + ("s" if len(usable_chat_endpoints) != 1 else ""),
                critical=True,
            )
        )
    else:
        # Name the actual blocker so the operator knows what to fix: nothing set up at
        # all, vs. a usable endpoint exists but is disabled, vs. none can drive tools.
        if not endpoints:
            chat_detail = "no provider configured"
        elif any(e.native_tools for e in endpoints):
            # Tool-calling endpoint(s) exist but none are enabled — re-enabling one fixes
            # chat, even when a non-tool endpoint happens to be enabled.
            chat_detail = "all tool-calling endpoints disabled"
        else:
            chat_detail = "no tool-calling endpoint"
        capabilities.append(
            Capability(
                key="chat_model",
                label="CHAT MODEL",
                status="alert",
                detail=chat_detail,
                critical=True,
                remediation_href="/models",
                remediation_label="CONFIGURE",
            )
        )
    # Embeddings — present ⇒ hybrid recall; absent ⇒ keyword-only (degraded, not down).
    # While a model change is being re-embedded, recall is partially degraded until it
    # lands, so the detail reflects the in-flight reindex.
    reindexing = deps.embedding_reindexer(request).status().state == "running"
    if embedding_configured and reindexing:
        embedding_detail = "re-indexing…"
    elif embedding_configured:
        embedding_detail = "hybrid recall"
    else:
        embedding_detail = "keyword-only recall"
    capabilities.append(
        Capability(
            key="embeddings",
            label="EMBEDDINGS",
            status="nominal" if embedding_configured else "warn",
            detail=embedding_detail,
            remediation_href=None if embedding_configured else "/models/embedding",
            remediation_label=None if embedding_configured else "CONFIGURE",
        )
    )
    # Execution sandbox — runs in the platform VM, so it is available once the VM is up;
    # absent ⇒ disabled (no host fallback).
    if not sandbox_present:
        sandbox_detail = "disabled"
    elif vm_ready:
        sandbox_detail = "platform VM"
    elif vm is not None and vm.failure is not None:
        sandbox_detail = "VM unavailable"
    else:
        sandbox_detail = "VM starting"
    capabilities.append(
        Capability(
            key="sandbox",
            label="CODE SANDBOX",
            status="nominal" if sandbox_present and vm_ready else "warn",
            detail=sandbox_detail,
        )
    )
    # Web search — the backend's managed SearXNG (or an operator-configured provider that
    # overrides it) ⇒ search available; neither ⇒ disabled (degraded, not down — e.g. the
    # platform VM is not up, or the instance is still booting).
    if offline_active:
        search_detail = "offline mode — paused"
    elif provider_enabled:
        search_detail = "SearXNG configured"
    elif managed_search_ready:
        search_detail = "SearXNG (managed)"
    elif not vm_ready:
        search_detail = "VM not ready — disabled"
    else:
        search_detail = "not running — disabled"
    capabilities.append(
        Capability(
            key="web_search",
            label="WEB SEARCH",
            status="warn" if offline_active or not web_search_configured else "nominal",
            detail=search_detail,
        )
    )
    # Web fetch — a separate capability from search: a headless browser in the platform
    # VM renders pages. It can be down independently (VM not up, browser not answering,
    # still bringing up), so it gets its own row rather than being folded into web search.
    fetch_available = deps.browser(request).available
    if offline_active:
        fetch_detail = "offline mode — paused"
    elif fetch_available:
        fetch_detail = "VM browser"
    elif not vm_ready:
        fetch_detail = "VM not ready — unavailable"
    else:
        fetch_detail = "not running — unavailable"
    capabilities.append(
        Capability(
            key="web_fetch",
            label="WEB FETCH",
            status="nominal" if fetch_available and not offline_active else "warn",
            detail=fetch_detail,
        )
    )
    # Out-of-band notification channels — email and push (`XC-DEG-3`). These are how an
    # unattended run's approval request and a reminder reach the operator when the app
    # isn't open (`AE-3.2`, `TASK-6`), so whether they're actually configured is worth
    # seeing. Each channel decides its own status/detail; this route only maps the shape.
    # A channel is never critical — losing it degrades to in-app-only, never down.
    for channel in await deps.notifications(request).channel_health(OPERATOR_ID):
        capabilities.append(
            Capability(
                key=channel.key,
                label=channel.label,
                status=channel.status,
                detail=channel.detail,
                remediation_href=None if channel.configured else "/settings",
                remediation_label=None if channel.configured else "CONFIGURE",
            )
        )

    return Overview(
        version=get_settings().version,
        endpoint_count=len(endpoints),
        conversation_count=conversation_count,
        memory_count=memory_count,
        active_run_count=len(active_runs),
        capabilities=capabilities,
    )
