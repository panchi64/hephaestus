"""Capability health — what the workspace can do right now, with backend-decided status.

Shared by the home overview and the Health page, so both render the same rows. The
policy for what counts as nominal/degraded/down lives here, not in the frontend.
"""

from __future__ import annotations

import asyncio

from fastapi import Request
from pydantic import BaseModel

from core.health import HealthStatus
from routes import deps
from routes.deps import OPERATOR_ID
from services.searxng import SearxngState

_MANAGED_SEARCH_DETAIL: dict[SearxngState, str] = {
    "off": "turned off",
    "waiting_vm": "starting",
    "starting": "starting",
    "slow": "slow to start — still waiting",
    "crashed": "crashed on start — see backend log",
    "failed": "could not start — see backend log",
    "ready": "SearXNG (managed)",
}


def search_detail(
    *, offline: bool, provider_enabled: bool, vm_ready: bool, managed: SearxngState
) -> str:
    """The web-search row's detail. An enabled provider wins over the managed instance."""
    if offline:
        return "offline mode — paused"
    if provider_enabled:
        return "SearXNG configured"
    if managed != "ready" and not vm_ready:
        return "VM not ready — disabled"
    return _MANAGED_SEARCH_DETAIL[managed]


class Capability(BaseModel):
    """A capability the workspace exposes, with backend-decided health.

    ``status``/``detail`` are rendered verbatim — the frontend does not decide
    what counts as nominal/degraded/down. ``critical`` marks a capability the
    workspace cannot function without (drives the overall-status flag)."""

    # Stable id: "chat_model", "embeddings", "sandbox", "web_search",
    # "web_fetch", or a notification channel's key.
    key: str
    label: str
    status: HealthStatus
    detail: str
    critical: bool = False
    remediation_href: str | None = None
    remediation_label: str | None = None


async def build_capabilities(request: Request) -> list[Capability]:
    """Every capability row, in display order: chat model, embeddings, sandbox, web
    search, web fetch, then one row per notification channel."""
    models = deps.models(request)
    roles, endpoints, search_providers = await asyncio.gather(
        models.list_roles(OPERATOR_ID),
        models.list_endpoints(OPERATOR_ID),
        deps.search(request).list_providers(OPERATOR_ID),
    )

    # The chat (`main`) model is chosen live from the top-bar picker, not bound
    # here — so the precondition the workspace can't function without is simply
    # that a usable chat endpoint exists. `main` requires native tool-calling
    # (enforced at resolve), so only such endpoints count.
    usable_chat_endpoints = [e for e in endpoints if e.native_tools and e.enabled]
    embedding_configured = bool(roles.get("embedding"))
    machines = deps.machines(request)
    platform = machines.platform if machines is not None else None
    vm = deps.vm(request)
    vm_ready = vm is not None and vm.available
    provider_enabled = any(p.enabled for p in search_providers)
    searxng = deps.searxng(request)
    managed_search_ready = searxng.base_url is not None
    web_search_configured = provider_enabled or managed_search_ready
    # Offline mode suspends both web capabilities (their VM services are stopped to save
    # resources); when active it's the reason the rows are down, so it overrides the
    # generic VM detail below.
    offline_active = deps.offline(request).state().effective_offline

    capabilities: list[Capability] = []
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
            # Re-enabling a tool-calling endpoint fixes chat, even when a non-tool
            # endpoint happens to be enabled.
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
    # Commands run in each conversation's VM, so they are available once the image is
    # built; absent ⇒ disabled (no host fallback).
    if platform is None:
        sandbox_detail = "disabled"
    elif platform.available:
        sandbox_detail = "a VM per conversation"
    elif platform.failure is not None:
        sandbox_detail = "VM unavailable"
    elif platform.image.building:
        sandbox_detail = "VM building image"
    else:
        sandbox_detail = "VM starting"
    capabilities.append(
        Capability(
            key="sandbox",
            label="CODE SANDBOX",
            status="nominal" if platform is not None and platform.available else "warn",
            detail=sandbox_detail,
        )
    )
    # Web search without a provider or a ready managed instance is degraded, not down.
    capabilities.append(
        Capability(
            key="web_search",
            label="WEB SEARCH",
            status="warn" if offline_active or not web_search_configured else "nominal",
            detail=search_detail(
                offline=offline_active,
                provider_enabled=provider_enabled,
                vm_ready=vm_ready,
                managed=searxng.state,
            ),
        )
    )
    # Web fetch is its own row: the VM's headless browser can be down while search is up.
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
    # Out-of-band notification channels (`XC-DEG-3`) carry an unattended run's approval
    # request and a reminder when the app isn't open (`AE-3.2`, `TASK-6`). Each channel
    # decides its own status/detail; never critical — losing one degrades to in-app only.
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
    return capabilities
