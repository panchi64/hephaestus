"""Manifest discovery/order, the service container, and the run-terminal dispatch."""

from __future__ import annotations

import asyncio
from typing import cast, override

import pytest

from harness.discovery import discover_manifests, order_manifests
from harness.manifest import FeatureManifest, ServiceContainer
from harness.run_terminal import RunTerminalDispatcher
from runs.run import Run
from runs.stream import RunStream


def test_order_respects_after_edges_and_breaks_ties_by_name():
    manifests = [
        FeatureManifest(name="tasks", after=("uploads",)),
        FeatureManifest(name="corpus"),
        FeatureManifest(name="uploads", after=("corpus",)),
        FeatureManifest(name="memory"),
    ]
    ordered = [m.name for m in order_manifests(manifests)]
    assert ordered.index("corpus") < ordered.index("uploads") < ordered.index("tasks")
    assert ordered[:2] == ["corpus", "memory"]  # the tie is name-sorted, not input-sorted


def test_order_rejects_duplicates_and_unknown_edges():
    with pytest.raises(ValueError, match="duplicate"):
        _ = order_manifests([FeatureManifest(name="a"), FeatureManifest(name="a")])
    with pytest.raises(ValueError, match="unknown"):
        _ = order_manifests([FeatureManifest(name="a", after=("ghost",))])


def test_discovered_manifests_are_valid_and_ordered():
    """Whatever lives in harness/manifests/ right now: unique names, known edges,
    every `after` producer strictly before its consumer."""
    manifests = discover_manifests()
    seen: set[str] = set()
    for manifest in manifests:
        assert set(manifest.after) <= seen, f"{manifest.name} builds before a dependency"
        seen.add(manifest.name)


def test_provider_assembly_order_is_deterministic_and_doc_state_is_prompt_context():
    """The assembled instruction/prompt-context provider order is pinned by discovery
    (topo + name-sorted ties) — instructions render at the head of every request, so a
    provider order that varied between boots would silently invalidate the inference
    engine's cached prompt prefix."""
    first = discover_manifests()
    second = discover_manifests()
    assert [m.name for m in first] == [m.name for m in second]
    assert [m.instructions for m in first] == [m.instructions for m in second]
    assert [m.prompt_context for m in first] == [m.prompt_context for m in second]


def test_the_browser_manifest_contributes_its_brief():
    """`browse_instructions` was written, documented in three places and unit-tested, and
    registered nowhere — so eighteen browser tools shipped with no account of how they fit
    together. Presence on the manifest is the whole wiring, so this is what pins it."""
    from tools.browse import browse_instructions

    browser = next(m for m in discover_manifests() if m.name == "browser")
    assert browse_instructions in browser.instructions


#: Functions in `tools/` named `*_instructions` that are deliberately *not* a feature's
#: contribution. Keep the reason with the name — an entry here is a claim that nothing is
#: missing, and the test below is only as good as this list is honest.
_NOT_MANIFEST_PROVIDERS = {
    # A *factory* that builds the provider; `agent/factory.py` registers its result from
    # the assembled dormant mapping, because what is dormant is decided by assembly rather
    # than contributed by any one feature.
    "tools.tool_search.dormant_index_instructions",
    # Seeded directly by `app.py` alongside the manifests': the project's own brief belongs
    # to no single feature.
    "tools.repo.repo_instructions",
}


def test_every_instruction_provider_in_tools_is_registered_or_listed():
    """A provider reaches a prompt only by being on a manifest, and nothing fails when one
    isn't — it just silently never renders, which is how `browse_instructions` stayed dead
    through three docs asserting it shipped. So enumerate them and require each to be either
    registered or explicitly excused."""
    import importlib
    import pkgutil

    import tools

    registered = {
        f"{provider.__module__}.{provider.__name__}"
        for manifest in discover_manifests()
        for provider in manifest.instructions
    }
    found: set[str] = set()
    for info in pkgutil.iter_modules(tools.__path__, prefix="tools."):
        module = importlib.import_module(info.name)
        for name, value in cast(dict[str, object], vars(module)).items():
            if name.endswith("_instructions") and callable(value):
                # Skip re-exports: attribute the function to the module that defines it.
                if getattr(value, "__module__", None) == info.name:
                    found.add(f"{info.name}.{name}")

    assert found, "found no *_instructions functions at all — the walk is broken, not clean"
    unaccounted = found - registered - _NOT_MANIFEST_PROVIDERS
    assert not unaccounted, (
        f"these instruction providers are defined but never registered: {sorted(unaccounted)}. "
        "Add them to a manifest's `instructions`, or to _NOT_MANIFEST_PROVIDERS with the reason."
    )


class _Memory:
    pass


def test_container_get_is_loud_and_optional_is_quiet():
    container = ServiceContainer()
    with pytest.raises(LookupError, match="_Memory"):
        _ = container.get(_Memory)
    assert container.get_optional(_Memory) is None
    instance = _Memory()
    container.add(instance)
    assert container.get(_Memory) is instance
    with pytest.raises(LookupError, match="already registered"):
        container.add(_Memory())


class _WatchedStream(RunStream):
    def __init__(self, subscribers: int) -> None:
        super().__init__()
        self._watchers: int = subscribers

    @property
    @override
    def subscriber_count(self) -> int:
        return self._watchers


def _run(subscribers: int = 0) -> Run:
    return Run(id="run-1", kind="chat", owner_id="o", stream=_WatchedStream(subscribers))


async def test_dispatcher_runs_sync_inline_then_async_as_tracked_tasks():
    dispatcher = RunTerminalDispatcher()
    log: list[str] = []
    dispatcher.add_sync(lambda run: log.append(f"sync:{run.id}"))

    async def notice(run: Run, watched: bool) -> None:
        log.append(f"async:{run.id}:watched={watched}")

    dispatcher.add(notice)
    run = _run(subscribers=2)
    dispatcher(run)
    assert log == ["sync:run-1"]  # sync ran inline; async is scheduled, not run
    assert len(dispatcher.tasks) == 1
    _ = await asyncio.gather(*dispatcher.tasks)
    assert log == ["sync:run-1", "async:run-1:watched=True"]


async def test_dispatcher_isolates_hook_failures():
    dispatcher = RunTerminalDispatcher()
    log: list[str] = []

    def bad_sync(_run: Run) -> None:
        raise RuntimeError("sync boom")

    async def bad_async(_run: Run, _watched: bool) -> None:
        raise RuntimeError("async boom")

    async def good(_run: Run, _watched: bool) -> None:
        log.append("good")

    dispatcher.add_sync(bad_sync)
    dispatcher.add(bad_async)
    dispatcher.add(good)
    dispatcher(_run())
    _ = await asyncio.gather(*dispatcher.tasks)
    assert log == ["good"]


async def test_dispatcher_drain_cancels_pending_tasks():
    dispatcher = RunTerminalDispatcher()
    parked = asyncio.Event()

    async def forever(_run: Run, _watched: bool) -> None:
        parked.set()
        _ = await asyncio.Event().wait()

    dispatcher.add(forever)
    dispatcher(_run())
    _ = await parked.wait()
    await dispatcher.drain()
    assert not dispatcher.tasks
