"""Find every feature manifest — presence is registration.

Core features are the modules in ``harness/manifests/``; built-in plugins are the packages
in ``plugins/``. Each exposes a ``MANIFEST``. Order is a deterministic topological sort over
the manifests' ``after`` edges (ties broken by name), so construction order follows declared
dependencies — never a central list, never import order luck.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterable
from graphlib import TopologicalSorter

import plugins as plugins_pkg
from harness import manifests as manifests_pkg
from harness.declarations import declare
from harness.manifest import FeatureManifest


def order_manifests(manifests: Iterable[FeatureManifest]) -> tuple[FeatureManifest, ...]:
    """Dependency (`after`) order, name-sorted ties — deterministic, never import luck.

    A manifest that composes turns is also ordered after every plugin that doesn't, so it
    stops first and no turn it starts outlives a plugin's services."""
    found: dict[str, FeatureManifest] = {}
    for manifest in manifests:
        if manifest.name in found:
            raise ValueError(f"duplicate feature manifest name: {manifest.name}")
        found[manifest.name] = manifest
    leaves = [name for name, m in found.items() if m.plugin is not None and not m.composes_turns]
    sorter: TopologicalSorter[str] = TopologicalSorter()
    for name, manifest in found.items():
        missing = [dep for dep in manifest.after if dep not in found]
        if missing:
            raise ValueError(f"manifest {name!r} is `after` unknown manifests: {missing}")
        implied = [leaf for leaf in leaves if leaf != name] if manifest.composes_turns else []
        sorter.add(name, *manifest.after, *implied)
    ordered: list[FeatureManifest] = []
    sorter.prepare()
    while sorter.is_active():
        ready = sorted(sorter.get_ready())
        for name in ready:
            ordered.append(found[name])
            sorter.done(name)
    return tuple(ordered)


def discover_manifests() -> tuple[FeatureManifest, ...]:
    """Every manifest, ordered, with its static declarations pushed into core's tables."""
    found: list[FeatureManifest] = []
    for info in pkgutil.iter_modules(manifests_pkg.__path__):
        if info.name.startswith("_"):
            continue
        manifest = _manifest_of(f"{manifests_pkg.__name__}.{info.name}")
        if manifest.plugin is not None:
            raise TypeError(f"core feature {manifest.name!r} declares itself a plugin")
        found.append(manifest)
    for info in pkgutil.iter_modules(plugins_pkg.__path__):
        if not info.ispkg:
            continue
        manifest = _manifest_of(f"{plugins_pkg.__name__}.{info.name}")
        if manifest.plugin is None:
            raise TypeError(f"plugins/{info.name} does not declare its PluginInfo")
        found.append(manifest)
    ordered = order_manifests(found)
    declare(ordered)
    return ordered


def _manifest_of(module_name: str) -> FeatureManifest:
    module = importlib.import_module(module_name)
    manifest = getattr(module, "MANIFEST", None)
    if not isinstance(manifest, FeatureManifest):
        raise TypeError(f"{module_name} does not expose a MANIFEST")
    return manifest
