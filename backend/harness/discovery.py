"""Find every feature manifest — presence in ``harness/manifests/`` is registration.

The walk imports each module in the package and collects its ``MANIFEST``. Order is
a deterministic topological sort over the manifests' ``after`` edges (ties broken
by name), so construction order follows declared dependencies — never a central
list, never import order luck.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterable
from graphlib import TopologicalSorter

from harness import manifests as manifests_pkg
from harness.declarations import declare
from harness.manifest import FeatureManifest


def order_manifests(manifests: Iterable[FeatureManifest]) -> tuple[FeatureManifest, ...]:
    """Dependency (`after`) order, name-sorted ties — deterministic, never import luck."""
    found: dict[str, FeatureManifest] = {}
    for manifest in manifests:
        if manifest.name in found:
            raise ValueError(f"duplicate feature manifest name: {manifest.name}")
        found[manifest.name] = manifest
    sorter: TopologicalSorter[str] = TopologicalSorter()
    for name, manifest in found.items():
        missing = [dep for dep in manifest.after if dep not in found]
        if missing:
            raise ValueError(f"manifest {name!r} is `after` unknown manifests: {missing}")
        sorter.add(name, *manifest.after)
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
        module = importlib.import_module(f"{manifests_pkg.__name__}.{info.name}")
        manifest = getattr(module, "MANIFEST", None)
        if not isinstance(manifest, FeatureManifest):
            raise TypeError(f"harness/manifests/{info.name}.py does not expose a MANIFEST")
        found.append(manifest)
    ordered = order_manifests(found)
    declare(ordered)
    return ordered
