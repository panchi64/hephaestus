"""Shared test helpers: a booted app + client, and an SSE event collector."""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

import httpx
from pydantic_ai.models.test import TestModel

from agent import ParkedTurn
from app import create_app
from core.config import Settings
from runs import EventBody, Run
from services.providers.base import ModelLimits
from services.providers.openai_compat import OpenAICompatProvider

if TYPE_CHECKING:
    from pydantic_ai import Agent

    from tools import RunDeps

#: The window the stub provider reports. It lives on the *provider*, not on the
#: ResolvedModel a test builds, because that is where a window comes from in
#: production — see `stub_resolution`.
STUB_CONTEXT_WINDOW = 128_000
_STUB_PROVIDER_ID = "test-stub"


class _StubProvider(OpenAICompatProvider):
    """A provider adapter that reports a context window and no other limit.

    Exists so a stubbed resolution still travels the real discovery path. A test that
    hard-coded `context_window=` onto its `ResolvedModel` would satisfy the chat
    route's window requirement while skipping the code that satisfies it in
    production — the provider lookup, the manual-value-wins precedence, and the
    memoization — leaving all of it unexercised by any route test.

    Only the limits are overridden; the rest is the OpenAI-compatible adapter's, and
    resolution is stubbed before it ever builds a model."""

    id = _STUB_PROVIDER_ID
    display_name = "Test stub"
    requires_key = False

    async def model_limits(
        self, base_url: str, api_key: str | None, model: str, *, client=None
    ) -> ModelLimits:
        return ModelLimits(context_window=STUB_CONTEXT_WINDOW)


async def stub_resolution(registry, model, *, reasoning_off=None):
    """A ``ResolvedModel`` whose window was **discovered**, for a test that stubs
    resolution but still wants the window to arrive the way it really does.

    ``registry`` is the ``ModelRegistry`` the patched method was called on, so the
    lookup runs against the same instance (and the same cache) production uses."""
    from services.registry import ResolvedModel

    spec = _stub_spec()
    [resolved] = await registry._with_model_limits([spec])
    return ResolvedModel(
        model=model,
        reasoning_off=reasoning_off or {},
        context_window=resolved.context_window,
    )


def _stub_spec():
    from services.llm import EndpointSpec

    # `context_window=None` is the point: it is what sends `_with_model_limits` to
    # the provider instead of short-circuiting on an operator-set value.
    return EndpointSpec(
        base_url="http://stub.invalid/v1",
        model="stub-model",
        provider=_STUB_PROVIDER_ID,
        context_window=None,
    )


def register_stub_provider(monkeypatch) -> None:
    """Add the stub adapter to the provider registry for one test.

    Through ``monkeypatch`` on the built registry dict, so it is removed afterwards —
    a provider id leaking across tests would make `all_providers()` (and the
    `/models/providers` listing it serves) depend on test order."""
    from services import providers

    registry = dict(providers._registry())
    registry[_STUB_PROVIDER_ID] = _StubProvider()
    monkeypatch.setattr(providers, "_PROVIDERS", registry)


def is_fold_request(messages) -> bool:
    """Whether a model request is a compaction summary — the turn's replay with the
    compaction instructions appended as the last user message."""
    from pydantic_ai import ModelRequest, UserPromptPart

    from prompts.compaction import COMPACT_INSTRUCTIONS

    last = messages[-1] if messages else None
    return isinstance(last, ModelRequest) and any(
        isinstance(part, UserPromptPart) and part.content == COMPACT_INSTRUCTIONS
        for part in last.parts
    )


def fold_aware_model(*, answer: str = "the answer", summary: str = "FOLDED AWAY"):
    """A main model that writes ``summary`` when asked to fold the thread and ``answer``
    to anything else — the summary is written by the thread's own model now, so a test
    driving a turn that folds needs one model that can tell the two requests apart."""
    from pydantic_ai import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel

    def _text(messages) -> str:
        return summary if is_fold_request(messages) else answer

    async def respond(messages, info):
        return ModelResponse(parts=[TextPart(content=_text(messages))])

    async def stream(messages, info):
        yield _text(messages)

    return FunctionModel(respond, stream_function=stream)


class FoldArgs(TypedDict):
    agent: Agent
    deps: RunDeps


def fold_with(summary: str, *, categories=None) -> FoldArgs:
    """The ``agent``/``deps`` pair ``compact_conversation`` writes a summary with: a turn's
    agent on a model that answers the fold with ``summary``, and the deps a turn would run
    it with. ``categories`` defaults to none, so no tool is offered."""
    from agent.factory import build_agent
    from agent.turn import turn_deps
    from runs import Run, RunStream

    run = Run(id="fold", kind="chat", owner_id="operator", stream=RunStream())
    return {
        "agent": build_agent(
            fold_aware_model(summary=summary),
            categories={} if categories is None else categories,
        ),
        "deps": turn_deps(run, conversation_id="c"),
    }


def patch_model_resolution(monkeypatch, *, output_text: str = "hi", call_tools=()):
    """Point registry resolution at a ``TestModel`` so route tests run without a live
    model server. One patch covers every caller: ``resolve_detailed`` is the single
    entry point that builds a chat model, and ``resolve_background`` delegates to it,
    so the chat route's ``main`` and the background (verify/title) model both come
    through here. ``call_tools=()`` keeps it a plain text turn — the default catalog
    has an approval-gated tool that would otherwise park the run."""
    from services.registry import ModelRegistry

    register_stub_provider(monkeypatch)

    def _model() -> TestModel:
        return TestModel(custom_output_text=output_text, call_tools=list(call_tools))

    async def resolve_detailed(self, role, **kwargs):
        return await stub_resolution(self, _model())

    monkeypatch.setattr(ModelRegistry, "resolve_detailed", resolve_detailed)


async def granting_store(owner: str, conv: str, *tool_names: str, ttl_s: float = 3600):
    """An ``ApprovalGrantStore`` on a throwaway DB that pre-grants ``tool_names`` in
    ``conv``. Lets a ``TestModel``-driven wiring test (which calls every offered tool)
    drive an AE-3.8-gated recall tool through to its service — the active grant makes the
    engine auto-approve it inline instead of parking on the approval prompt."""
    from core.db import init_db, make_engine
    from services.approval_grants import ApprovalGrantStore

    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    store = ApprovalGrantStore(engine, ttl_s)
    for name in tool_names:
        await store.grant(owner, conv, name)
    return store


def swap_tool_catalog(app, categories) -> None:
    """Replace the booted app's assembled tool catalog **in place**. The mapping is one
    shared object — the chat route, the approval-resume path, and the scheduler's
    executor all hold the same dict the app assembled — so mutating its contents (not
    rebinding `app.state.tool_categories`) is what reaches every path."""
    app.state.tool_categories.clear()
    app.state.tool_categories.update(categories)


def full_tool_categories():
    """The complete category mapping a real app assembles — core plus every manifest's
    `toolsets` export, via the same discovery the app uses — for tests that measure
    the whole catalog without booting an app."""
    from harness.discovery import discover_manifests
    from tools.toolsets import core_categories

    categories = core_categories()
    for manifest in discover_manifests():
        for name, factory in manifest.toolsets:
            categories[name] = factory()
    return categories


@asynccontextmanager
async def client_app(*, auth_enabled: bool = False, passphrase: str | None = "test-passphrase"):
    """A booted app + async client, backed by a throwaway in-memory DB.

    An in-memory SQLite URL plus a temp data dir (for the keyfile) keep tests off
    the real ``data/`` dir. By default auth is off and a passphrase unlocks the
    vault at boot, so feature endpoints are reachable without a token; the auth
    tests pass ``auth_enabled=True, passphrase=None`` to exercise setup/login.
    """
    with tempfile.TemporaryDirectory() as tmp:
        settings = Settings(
            db_url="sqlite:///:memory:",
            data_dir=Path(tmp),
            # Coding-mode worktrees live *outside* `data_dir` by design (the host-command
            # fence denies reads of the whole data directory), so they need their own
            # redirect here or a test cuts real git worktrees into the operator's home.
            worktrees_dir=Path(tmp) / "worktrees",
            auth_enabled=auth_enabled,
            unlock_passphrase=passphrase,
            # No VM side effects in tests, regardless of the host: the managed
            # SearXNG / web-fetch browser would start in the platform VM at boot, and
            # the sandbox would otherwise depend on it. Tests that need
            # any of these inject it directly (e.g. app.state.sandbox = a fake).
            searxng_enabled=False,
            web_fetch_enabled=False,
            sandbox_enabled=False,
            # Assume online without touching the network, so the offline-mode monitor's
            # boot probe doesn't make a real connection (and the web rows stay nominal).
            offline_check_enabled=False,
        )
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                yield client, app


async def collect_sse_events(client, run_id, *, last_event_id=None):
    """Drain a run's SSE stream to a list of decoded event envelopes."""
    params = {} if last_event_id is None else {"last_event_id": last_event_id}
    events = []
    async with client.stream("GET", f"/runs/{run_id}/events", params=params) as resp:
        assert resp.status_code == 200
        async for line in resp.aiter_lines():
            if line.startswith("data:"):
                events.append(json.loads(line[len("data:") :].strip()))
    return events


def group_running(pgid: int) -> bool:
    """Whether a process group has a member that is not a zombie. A container whose init
    does not reap orphans leaves a killed child as a zombie, which still answers signal 0."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # macOS answers EPERM, not ESRCH, for a group whose only members are zombies.
        return False
    proc = Path("/proc")
    if not proc.is_dir():
        return True
    for stat in proc.glob("[0-9]*/stat"):
        try:
            state, _ppid, group = stat.read_text().rsplit(")", 1)[1].split()[:3]
        except OSError, IndexError, ValueError:
            continue
        if int(group) == pgid and state != "Z":
            return True
    return False


def parked_turn(run: Run) -> ParkedTurn:
    payload = run.parked_payload
    assert isinstance(payload, ParkedTurn)
    return payload


def run_bodies(run: Run) -> list[EventBody]:
    return [e.body for e in run.stream.replay()]


def bodies_of[T](run: Run, kind: type[T]) -> list[T]:
    return [b for b in run_bodies(run) if isinstance(b, kind)]


def first_body[T](run: Run, kind: type[T]) -> T:
    return next(b for b in run_bodies(run) if isinstance(b, kind))
