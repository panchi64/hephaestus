"""What a feature declares to the harness.

A feature stays split across the layers (`routes/`, `tools/`, `services/`, `models/`);
its cohesion lives in one `FeatureManifest` in ``harness/manifests/<name>.py``, declaring
its routers, API scopes, services and hooks. The harness discovers manifests by walking
that package (presence is registration, as in ``models/_discovery``), so a feature never
edits a central list.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from core.container import ServiceContainer

if TYPE_CHECKING:
    from fastapi import APIRouter, FastAPI
    from pydantic_ai import AbstractToolset
    from sqlalchemy import Engine

    from core.api_scopes import ScopeClaim
    from core.config import Settings
    from core.vault import Vault
    from harness.lifecycle import LifecycleRegistry
    from harness.run_terminal import RunTerminalHook, SyncRunTerminalHook
    from runs.lanes import RunKind
    from services.credential_store import ServiceInfo
    from services.permissions.projections import Projection
    from services.tool_policy import AvailabilityCheck, CategoryAvailability
    from services.tool_sensitivity import Sensitivity
    from tools import InstructionProvider, PromptContextProvider, RunDeps
    from tools.platform import PlatformSource

__all__ = [
    "DormantCategory",
    "FeatureManifest",
    "FeatureRuntime",
    "HarnessContext",
    "PluginInfo",
    "ServiceContainer",
]


@dataclass(frozen=True)
class DormantCategory:
    """A tool category whose schemas the model loads only once it wants them.

    Tool definitions ride in every prompt whether used or not (the browser alone outweighs
    the standing brief). A dormant category hides them behind Pydantic AI's deferred
    loading until the model asks by name through ``search_tools``, after which they stay
    for the conversation. The model reveals it; there is no operator switch. The chassis
    only tells it the group exists, via the one-line ``summary`` in the standing index.
    """

    # The category the toolset registers under, as in ``toolsets`` (the tool-name prefix).
    category: str
    # One line in the model's index, concrete enough that a turn needing the group
    # recognises it without loading anything.
    summary: str

    @staticmethod
    def summaries(entries: Iterable[DormantCategory]) -> dict[str, str]:
        """The declarations as category → summary, the mapping the engine composes a turn from.

        Assembly extends a list as manifests land; the engine wants one mapping for the
        index and the ``search_tools`` corpus. Kept here so composers don't each respell it.
        """
        return {entry.category: entry.summary for entry in entries}


@dataclass(frozen=True)
class PluginInfo:
    """What makes a manifest a built-in plugin: the operator can see it and switch it off.

    A built-in plugin lives in ``backend/plugins/<name>/``. Switched off, its routes answer
    404, its tools and prompt blocks are withheld and its contributions are deactivated;
    its services stay built, so a parked run of its kind can still resume.
    """

    title: str
    description: str
    default_enabled: bool = True


@dataclass(frozen=True)
class HarnessContext:
    """What a feature's ``build`` sees: the core handles plus every service built before it
    (its ``after`` edges fix which)."""

    settings: Settings
    engine: Engine
    vault: Vault
    lifecycle: LifecycleRegistry
    services: ServiceContainer
    # The agent-facing capability bag (`RunDeps.caps`) assembled so far, for a feature that
    # composes agent runs (the task scheduler).
    capabilities: ServiceContainer
    # Core plus every enabled manifest's declarations, complete before any build runs, so a
    # feature composing agent runs gives the engine what an interactive turn gets.
    tool_categories: Mapping[str, AbstractToolset[RunDeps]] = field(default_factory=lambda: {})
    instruction_providers: tuple[InstructionProvider, ...] = ()
    prompt_context_providers: tuple[PromptContextProvider, ...] = ()
    # Every enabled manifest's ``network_tools``, unioned, for the offline-gate feature.
    network_tools: frozenset[str] = frozenset()
    # Every enabled manifest's ``dormant`` declarations, in assembly order.
    dormant_categories: tuple[DormantCategory, ...] = ()
    # Every ``available`` check resolved against its categories; a feature composing an
    # unattended turn passes it to ``services/tool_policy.effective_disabled_tools`` so
    # unconfigured categories are withheld as in an interactive turn.
    category_availability: tuple[CategoryAvailability, ...] = ()
    # For a feature that composes a turn like a request would (the inbox dispatcher). None
    # where a test builds a context by hand.
    app: FastAPI | None = None
    # Set once every manifest has built and registered its run-terminal hooks, for a startup
    # pass that ends runs and needs every feature to hear of it.
    built: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass(frozen=True)
class FeatureRuntime:
    """What a feature's ``build`` hands back for the harness to wire in."""

    # Instances other features resolve from the container, keyed by concrete type.
    services: tuple[object, ...] = ()
    # The subset the agent's tools may reach through ``RunDeps.caps``; a service not
    # exported is invisible to every tool (the shell exports nothing, by design).
    #
    # An entry is a bare instance (keyed by concrete type) or an ``(instance, as_type)``
    # pair keyed by an abstract type. Tools resolve capabilities by type, and one
    # implemented above ``tools/`` (anything composing a whole agent turn) can't be named
    # there without inverting the dependency order; declare the abstraction in
    # ``services/`` and register the implementation under it.
    capabilities: tuple[object | tuple[object, type[object]], ...] = ()
    # Names hung on ``app.state`` for ``routes/deps.py``'s accessors; transitional, shrinking
    # as those move onto the container.
    state: Mapping[str, object] = field(default_factory=lambda: {})
    # Run-terminal hooks (``harness/run_terminal.py``): sync ones run inline in the
    # registry's terminal transition, async ones as tracked tasks.
    run_terminal_sync: tuple[SyncRunTerminalHook, ...] = ()
    run_terminal: tuple[RunTerminalHook, ...] = ()
    # A built-in plugin's contributions: items of the types in ``services/plugins/
    # contributions``, each matched to its type by class and activated while the plugin is on.
    contributions: tuple[object, ...] = ()


@dataclass(frozen=True)
class FeatureManifest:
    """One feature, declared; ``harness/manifests/<name>.py`` exposes it as ``MANIFEST``."""

    name: str
    # Manifest names whose services this build resolves; the only ordering input.
    after: tuple[str, ...] = ()
    # Set on a built-in plugin (``backend/plugins/<name>/``), never on a core feature.
    plugin: PluginInfo | None = None
    # Registered at app assembly, before the lifespan runs.
    routers: tuple[APIRouter, ...] = ()
    # Inbound-token scope claims; a surface no claim covers stays token-unreachable.
    api_scopes: tuple[ScopeClaim, ...] = ()
    # Path prefixes exempt from the auth gate, for surfaces whose unguessable path token
    # is the credential.
    public_prefixes: tuple[str, ...] = ()
    # Kill-switch: False means routers are never registered and the build never runs.
    enabled: Callable[[Settings], bool] | None = None
    # (category, factory) pairs for the agent's catalog; factories so importing a manifest
    # builds nothing. A duplicate category name fails the boot.
    toolsets: tuple[tuple[str, Callable[[], AbstractToolset[RunDeps]]], ...] = ()
    # Categories from ``toolsets`` that start each conversation dormant: listed as usual
    # but with schemas withheld until the model asks by name. Declared here because the
    # feature shipping an expensive, rarely-wanted category knows it is both.
    dormant: tuple[DormantCategory, ...] = ()
    # Whether the operator has set the feature up, asked of the live service every turn and
    # answered for every category the manifest registers (availability belongs to the
    # backing service). A feature with no such state declares nothing and is always
    # offered. False withholds the whole category as the operator's switch does, so the
    # model never gets a `mail_search` with no mailbox and a dormant group drops out of the
    # index instead of advertising an empty reveal (`tools/tool_search.py`).
    available: AvailabilityCheck | None = None
    # Namespaced tool names gated at runtime (they raise ``ApprovalRequired`` mid-call, so
    # inspection can't find them); this feature's part of the approval-scope vocabulary.
    gated_tools: frozenset[str] = frozenset()
    # Namespaced tool names that need internet, withheld while offline. Declared by the
    # owning feature so the offline service needn't be edited for each new one (a
    # hard-coded set there would silently offer a dead tool).
    network_tools: frozenset[str] = frozenset()
    # Rows this feature adds to core's process-wide tables (``harness/declarations.py``),
    # so core never names a feature's tools, kinds or keys.
    # Namespaced tool name → what the tool can do.
    tool_sensitivity: Mapping[str, Sensitivity] = field(default_factory=lambda: {})
    # Namespaced tool name → how an approval card and the auto-reviewer describe a call.
    projections: Mapping[str, Projection] = field(default_factory=lambda: {})
    # Tool categories whose results may carry personal data, so they are never spilled
    # to plaintext scratch.
    personal_categories: frozenset[str] = frozenset()
    # Outbound service keys the operator can store for this feature.
    credentials: tuple[ServiceInfo, ...] = ()
    # Run kinds this feature submits turns under.
    run_kinds: Mapping[str, RunKind] = field(default_factory=lambda: {})
    # Notification kinds worth reaching the operator over out-of-band channels.
    out_of_band_kinds: frozenset[str] = frozenset()
    # Named sources ``platform_search`` can be pointed at, over corpus records this
    # feature registers at build.
    platform_sources: Mapping[str, PlatformSource] = field(default_factory=lambda: {})
    # Composes agent turns of its own. Discovery orders it after every plugin that doesn't,
    # so it stops first and a turn it starts can't outlive a plugin's services.
    composes_turns: bool = False
    # ── Which seam a block of text belongs on ────────────────────────────────────────
    #
    # A block byte-stable across a conversation's turns belongs in the head; one that churns
    # within it belongs in the tail. Not "small in head, large in tail", which is wrong in
    # both directions.
    #
    # Prompt caches (llama.cpp's longest common prefix per slot, vLLM's chained block
    # hashes, OpenAI's single implicit breakpoint) reuse leading tokens only while they match
    # the last request and re-read everything after the first difference:
    #
    # - Head: a stable block is free after the first request carrying it, however large. A
    #   changing one invalidates the entire history behind it every time.
    # - Tail: the tail isn't persisted, so turn N+1 replays turn N's prompt without it. A
    #   tail block costs one divergence per turn boundary, bounded by one turn's work and
    #   not growing with the thread; free within a turn, since providers resolve once in
    #   the prelude.
    #
    # So a large stable brief in the tail pays every turn, and a small volatile line in the
    # head discards the conversation each time it moves. `agent/prefix_watch.py` reports which.

    # Instruction providers registered on every agent the engine builds; each resolves its
    # capability from the run's bag and returns "" to no-op. They render at the head, so
    # must be byte-stable for a conversation. A block that comes and goes with a session is
    # still head material (it switches when something else already invalidated the prefix);
    # one that moves with the turn is not.
    instructions: tuple[InstructionProvider, ...] = ()
    # Like `instructions` re-resolved each turn and never persisted, but delivered at the
    # tail of the current user prompt: where content that changes within a conversation
    # goes, whatever its size, leaving the cached prefix intact.
    prompt_context: tuple[PromptContextProvider, ...] = ()
    # Constructs the feature's services at lifespan time. None ⇒ routes-only.
    build: Callable[[HarnessContext], Awaitable[FeatureRuntime]] | None = None
