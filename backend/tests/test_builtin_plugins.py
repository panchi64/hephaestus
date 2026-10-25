"""Built-in plugins: discovered from ``plugins/``, switched on and off live, and kept apart.

A new kind of contribution must be one module: registering a type is all it takes for a
built-in plugin to carry its items and have them brought up and stood down with it.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import override

import pytest

from core.db import init_db, make_engine
from harness.builtin_plugins import BuiltinPlugins
from harness.discovery import order_manifests
from harness.manifest import FeatureManifest, PluginInfo
from services.plugins.contributions.background import Background
from services.plugins.contributions.base import Described, TypedContribution
from services.plugins.contributions.registry import CONTRIBUTIONS
from services.settings_store import SettingsStore

from ._helpers import client_app
from ._json import json_list, obj

BACKEND = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Beacon:
    name: str
    lit: list[str] = field(default_factory=lambda: [], compare=False)


class Beacons(TypedContribution[Beacon]):
    @override
    async def start(self, plugin: str, items: tuple[Beacon, ...]) -> None:
        for item in items:
            item.lit.append(f"on:{plugin}")

    @override
    async def stop(self, plugin: str, items: tuple[Beacon, ...]) -> None:
        for item in items:
            item.lit.append(f"off:{plugin}")

    @override
    def describe_one(self, item: Beacon) -> Described:
        return {"name": item.name, "lit": bool(item.lit)}


_ = CONTRIBUTIONS.register(Beacons("beacons", "Beacons", Beacon))


def _store() -> SettingsStore:
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    return SettingsStore(engine)


def _plugin(name: str, *, default_enabled: bool = True) -> FeatureManifest:
    return FeatureManifest(
        name=name, plugin=PluginInfo(name.title(), "test", default_enabled=default_enabled)
    )


async def test_a_new_contribution_type_is_brought_up_and_stood_down_with_its_plugin():
    plugins = BuiltinPlugins(_store(), [_plugin("lighthouse")])
    await plugins.load()
    beacon = Beacon("north")
    await plugins.attach("lighthouse", (beacon,))
    assert beacon.lit == ["on:lighthouse"]

    await plugins.set_enabled("lighthouse", False)
    assert beacon.lit == ["on:lighthouse", "off:lighthouse"]
    assert not plugins.enabled("lighthouse")

    [view] = plugins.views()
    assert view.contributions == {"beacons": [{"name": "north", "lit": True}]}


async def test_the_switch_survives_a_restart():
    store = _store()
    first = BuiltinPlugins(store, [_plugin("lighthouse")])
    await first.load()
    await first.set_enabled("lighthouse", False)

    second = BuiltinPlugins(store, [_plugin("lighthouse")])
    await second.load()
    beacon = Beacon("north")
    await second.attach("lighthouse", (beacon,))
    assert not second.enabled("lighthouse")
    assert beacon.lit == []


async def test_a_plugin_off_by_default_stays_off_until_switched_on():
    plugins = BuiltinPlugins(_store(), [_plugin("lighthouse", default_enabled=False)])
    await plugins.load()
    beacon = Beacon("north")
    await plugins.attach("lighthouse", (beacon,))
    assert beacon.lit == []
    await plugins.set_enabled("lighthouse", True)
    assert beacon.lit == ["on:lighthouse"]


async def test_background_work_runs_only_while_switched_on():
    events: list[str] = []

    async def start() -> None:
        events.append("start")

    async def stop() -> None:
        events.append("stop")

    plugins = BuiltinPlugins(_store(), [_plugin("sync")])
    await plugins.load()
    await plugins.attach("sync", (Background("loop", start, stop),))
    await plugins.set_enabled("sync", False)
    await plugins.set_enabled("sync", True)
    await plugins.deactivate("sync")
    await plugins.deactivate("sync")
    assert events == ["start", "stop", "start", "stop"]


def test_an_item_of_no_registered_type_is_refused():
    with pytest.raises(TypeError):
        _ = CONTRIBUTIONS.kind_of(object())


def test_a_feature_composing_turns_is_ordered_after_every_plain_plugin():
    composer = FeatureManifest(name="aaa", composes_turns=True)
    ordered = [m.name for m in order_manifests([composer, _plugin("zzz"), _plugin("mmm")])]
    assert ordered.index("aaa") > ordered.index("zzz")
    assert ordered.index("aaa") > ordered.index("mmm")


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return found


def _sources() -> list[Path]:
    skip = {".venv", "tests", "migrations", "evals"}
    return [
        path for path in BACKEND.rglob("*.py") if not skip & set(path.relative_to(BACKEND).parts)
    ]


def test_nothing_outside_a_plugin_imports_one():
    for path in _sources():
        parts = path.relative_to(BACKEND).parts
        if parts[0] == "plugins":
            continue
        reached = {name for name in _imports(path) if name.startswith("plugins.")}
        assert not reached, f"{path.relative_to(BACKEND)} imports {sorted(reached)}"


def test_no_plugin_imports_another():
    for path in _sources():
        parts = path.relative_to(BACKEND).parts
        if parts[0] != "plugins" or len(parts) < 3:
            continue
        own = f"plugins.{parts[1]}"
        others = {
            name
            for name in _imports(path)
            if name.startswith("plugins.") and not name.startswith(f"{own}.") and name != own
        }
        assert not others, f"{path.relative_to(BACKEND)} imports {sorted(others)}"


async def test_switching_mail_off_withdraws_its_routes_and_back_on_restores_them():
    async with client_app() as (client, _app):
        assert (await client.get("/mail/accounts")).status_code == 200

        off = await client.put("/plugins/builtin/mail", json={"enabled": False})
        assert off.status_code == 200
        assert (await client.get("/mail/accounts")).status_code == 404
        listed = {obj(p)["name"]: obj(p) for p in json_list(await client.get("/plugins/builtin"))}
        assert listed["mail"]["enabled"] is False

        on = await client.put("/plugins/builtin/mail", json={"enabled": True})
        assert on.status_code == 200
        assert (await client.get("/mail/accounts")).status_code == 200
