"""What a feature declares to the harness — one file names all its layered pieces.

A feature stays split across the layers (`routes/` + `tools/` + `services/` +
`models/`), because the layer law is what keeps the dependency direction honest;
its *cohesion* lives here instead: a single `FeatureManifest` in
``harness/manifests/<name>.py`` declares the routers it serves, the API scopes it
claims, the services it builds, and the hooks it registers. The harness discovers
manifests by walking that package — presence is registration, the same contract as
``models/_discovery`` and the backup marker — so landing a feature never edits a
central list.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from core.container import ServiceContainer

if TYPE_CHECKING:
    from fastapi import APIRouter
    from pydantic_ai import AbstractToolset
    from sqlalchemy import Engine

    from core.api_scopes import ScopeClaim
    from core.config import Settings
    from core.vault import Vault
    from harness.lifecycle import LifecycleRegistry
    from harness.run_terminal import RunTerminalHook, SyncRunTerminalHook
    from services.tool_policy import AvailabilityCheck, CategoryAvailability
    from tools import InstructionProvider, PromptContextProvider, RunDeps

__all__ = [
    "DormantCategory",
    "FeatureManifest",
    "FeatureRuntime",
    "HarnessContext",
    "ServiceContainer",
]


@dataclass(frozen=True)
class DormantCategory:
    """A tool category whose schemas the model loads only once it wants them.

    A big category the average turn never touches is paid for on every request anyway —
    its definitions ride in the prompt whether or not they are used, and the browser
    alone costs more than the standing brief. Declaring the category dormant hides its
    tools behind Pydantic AI's deferred loading: they cost nothing until the model asks
    for them by name through ``search_tools``, and once it has, they stay for the rest
    of the conversation.

    **The model reveals it, not the operator** — there is no switch to flip and no
    heuristic guessing at relevance. What the chassis owes it is knowing the group is
    *there*, which is the one line ``summary`` supplies to the standing index.
    """

    # The category the manifest registers its toolset under — the same string its
    # ``toolsets`` entry uses, since that is what the tool names are prefixed with.
    category: str
    # One line, in the index the model reads: what this group of tools is for, concrete
    # enough that a turn needing it recognises it without loading anything.
    summary: str

    @staticmethod
    def summaries(entries: Iterable[DormantCategory]) -> dict[str, str]:
        """The declarations as the shape a turn is composed from: category → summary.

        The engine wants one mapping for both halves of the seam — the index the model
        reads and the corpus its ``search_tools`` reveals from — while assembly wants a
        list it can extend as each manifest lands. Every composer that bridges the two
        would otherwise spell the same comprehension, and a fourth would spell it
        slightly differently.
        """
        return {entry.category: entry.summary for entry in entries}


@dataclass(frozen=True)
class HarnessContext:
    """What a feature's ``build`` sees: the core handles plus every service built
    before it (its own ``after`` edges guarantee which those are)."""

    settings: Settings
    engine: Engine
    vault: Vault
    lifecycle: LifecycleRegistry
    services: ServiceContainer
    # The agent-facing capability bag (`RunDeps.caps`) as assembled so far — what a
    # feature composing agent runs (the task scheduler) hands to the engine.
    capabilities: ServiceContainer
    # The assembled tool-category mapping and instruction providers (core + every
    # enabled manifest's declarations, complete before any build runs) — so a feature
    # composing agent runs hands the engine exactly what an interactive turn gets.
    tool_categories: Mapping[str, AbstractToolset[RunDeps]] = field(default_factory=lambda: {})
    instruction_providers: tuple[InstructionProvider, ...] = ()
    prompt_context_providers: tuple[PromptContextProvider, ...] = ()
    # Every enabled manifest's ``network_tools``, unioned — for the feature that enforces
    # the offline gate, which is not the same feature as the ones declaring the tools.
    network_tools: frozenset[str] = frozenset()
    # Every enabled manifest's ``dormant`` declarations, in assembly order — the index
    # the agent is shown and the corpus its ``search_tools`` calls reveal from.
    dormant_categories: tuple[DormantCategory, ...] = ()
    # Every enabled manifest's ``available`` check, resolved against the categories it
    # registers — what a feature composing its own unattended turn passes to
    # ``services/tool_policy.effective_disabled_tools`` so an unconfigured category is
    # withheld from it exactly as it is from an interactive one.
    category_availability: tuple[CategoryAvailability, ...] = ()


@dataclass(frozen=True)
class FeatureRuntime:
    """What a feature's ``build`` hands back for the harness to wire in."""

    # Capability instances other features resolve from the container — keyed by
    # each instance's concrete type.
    services: tuple[object, ...] = ()
    # The subset of those instances the *agent's tools* may reach through
    # ``RunDeps.caps`` — the curated agent-facing boundary. A service not exported
    # here is invisible to every tool (the shell exports nothing, by design).
    #
    # An entry may be a bare instance (keyed by its concrete type, the common case) or
    # an ``(instance, as_type)`` pair, which keys it by an abstract type instead. That
    # exists for a specific structural reason: a tool resolves capabilities *by type*,
    # so a capability implemented in a layer **above** ``tools/`` — anything that composes
    # a whole agent turn, say — could not otherwise be reached, because naming its concrete
    # class in a tool would invert the dependency order. Declaring the abstraction in
    # ``services/`` and registering the implementation under it keeps the arrow pointing
    # down.
    capabilities: tuple[object | tuple[object, type[object]], ...] = ()
    # Names hung on ``app.state`` — the transitional seam ``routes/deps.py``'s
    # accessors read; shrinks as those accessors move onto the container.
    state: Mapping[str, object] = field(default_factory=lambda: {})
    # Run-terminal participation (see ``harness/run_terminal.py``): sync hooks run
    # inline in the registry's terminal transition, async hooks run as tracked tasks.
    run_terminal_sync: tuple[SyncRunTerminalHook, ...] = ()
    run_terminal: tuple[RunTerminalHook, ...] = ()


@dataclass(frozen=True)
class FeatureManifest:
    """One feature, declared. ``harness/manifests/<name>.py`` exposes it as
    ``MANIFEST``; everything else follows from discovery."""

    name: str
    # Manifest names whose services this build resolves — the only ordering input.
    after: tuple[str, ...] = ()
    # Routers registered at app assembly (before the lifespan runs).
    routers: tuple[APIRouter, ...] = ()
    # Inbound-token scope claims for this feature's surfaces (`AUTH-4`). A surface
    # no manifest (and no core claim) covers stays token-unreachable.
    api_scopes: tuple[ScopeClaim, ...] = ()
    # Path prefixes exempt from the auth gate — for surfaces whose unguessable
    # path token *is* the credential. Claiming one is a deliberate, visible act.
    public_prefixes: tuple[str, ...] = ()
    # Feature kill-switch: when it returns False the routers are never registered
    # and the build never runs.
    enabled: Callable[[Settings], bool] | None = None
    # The tool categories this feature contributes to the agent's catalog, as
    # (category, factory) pairs — declared as factories so importing a manifest
    # builds nothing. Assembled with the core categories at app startup; a duplicate
    # category name fails the boot loudly.
    toolsets: tuple[tuple[str, Callable[[], AbstractToolset[RunDeps]]], ...] = ()
    # Categories from ``toolsets`` above that start each conversation dormant: registered
    # and listed like every other, but with their schemas withheld from the model until it
    # asks for the group by name. Declared beside the toolset rather than in a list
    # somewhere central for the same reason ``network_tools`` is — the feature that ships
    # an expensive, rarely-wanted category is the one that knows it is both.
    dormant: tuple[DormantCategory, ...] = ()
    # Whether the operator has actually set this feature up — asked of the live service
    # on every turn, and answered for **every** category the manifest registers, because
    # availability is a fact about the backing service rather than about one group of
    # verbs over it. A feature with no such state (there is no way to *not* have a
    # filesystem) declares nothing and is always offered. A False answer withholds the
    # category the way the operator's own switch does, so the model is never handed a
    # `mail_search` with no mailbox behind it — and, since the whole category goes, a
    # dormant group drops out of the agent's index instead of advertising a reveal that
    # can only come back empty (`tools/tool_search.py`).
    available: AvailabilityCheck | None = None
    # Namespaced tool names whose approval gate is runtime-conditional (they raise
    # ``ApprovalRequired`` from inside the call, so inspection can't find them) —
    # this feature's contribution to the approval-scope vocabulary.
    gated_tools: frozenset[str] = frozenset()
    # Namespaced tool names that cannot work without internet access, so offline mode
    # withholds them from the agent while the link is down. Declared by the feature that
    # owns the tool rather than listed inside the offline service: offline mode's job is
    # to suspend network-dependent capabilities, and it should not have to be edited every
    # time a feature grows another one — which is exactly what a hard-coded set there
    # would require, silently offering a dead tool until someone remembered.
    network_tools: frozenset[str] = frozenset()
    # ── Which seam a block of text belongs on ────────────────────────────────────────
    #
    # **A block that is byte-stable across the turns of a conversation belongs in the head;
    # a block that churns within a conversation belongs in the tail.** Not "small in the
    # head, large in the tail", which is the reading that comes to mind first and is wrong
    # in both directions.
    #
    # The reason is the shape of every prompt cache worth having. llama.cpp matches a
    # longest common prefix per slot, vLLM chains block hashes, and the OpenAI wire has a
    # single implicit breakpoint over the whole rendered prefix — so each reuses a request's
    # leading tokens only while they are identical to what it saw last, and re-reads
    # everything from the first difference onward. That gives the two seams opposite
    # economics:
    #
    # - In the **head**, a stable block is free after the request that first carries it,
    #   however large it is — it joins the cached prefix and is never read again. A block
    #   that changes is not merely expensive for itself: it invalidates the entire history
    #   behind it, every turn it changes.
    # - In the **tail**, nothing is ever cached for the *next* turn, because the tail is not
    #   persisted: turn N+1 replays turn N's prompt without it. So a tail block costs one
    #   divergence at each turn boundary whether it changed or not — bounded at one turn's
    #   assistant work, and it does not grow with thread length. Free within a turn, since
    #   providers resolve once in the prelude and every step of the turn shares it.
    #
    # So a large, stable brief in the tail pays for itself on every turn forever, and a
    # small, volatile line in the head throws away the whole conversation each time it
    # moves. `agent/prefix_watch.py` reports which of the two has happened.

    # Dynamic instruction providers the engine registers on every agent it builds —
    # each resolves its capability from the run's bag and returns "" to no-op. These
    # render at the *head* of every request: byte-stable for the life of a conversation,
    # per the rule above. A block that comes and goes with a *session* is still head
    # material (it switches at the moment something else already invalidated the prefix);
    # one that moves with the turn is not.
    instructions: tuple[InstructionProvider, ...] = ()
    # Per-turn prompt-context providers — like `instructions` re-resolved fresh each
    # turn and never persisted, but delivered at the *tail* of the current turn's user
    # prompt. This is where content that changes within a conversation goes, whatever its
    # size: it leaves the request prefix (and so the engine's prompt cache) intact.
    prompt_context: tuple[PromptContextProvider, ...] = ()
    # Constructs the feature's services at lifespan time. None ⇒ routes-only.
    build: Callable[[HarnessContext], Awaitable[FeatureRuntime]] | None = None
