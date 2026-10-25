"""Shared setup for the plugin tests: a plugin bundle and a store on an in-memory database."""

from __future__ import annotations

from pathlib import Path

from core.db import init_db, make_engine
from core.vault import Vault
from services.plugins import PluginSecretStore, PluginStore
from services.revisions import RevisionStore
from services.skills import SkillStore

OWNER = "operator"

MANIFEST = """\
name: weather
description: Forecasts.
requirements: [httpx]
egress: [api.example.com]
secrets: {API_KEY: api.example.com}
tools:
  - name: forecast
    description: The forecast for a city.
    entry: weather:forecast
    sensitivity: read
    parameters:
      type: object
      properties: {city: {type: string}}
      required: [city]
"""

CODE = "def forecast(city):\n    return {'city': city, 'sky': 'clear'}\n"


def bundle(manifest: str = MANIFEST, code: str = CODE) -> dict[str, bytes]:
    return {"plugin.yaml": manifest.encode(), "weather.py": code.encode()}


async def make_store(tmp_path: Path) -> PluginStore:
    return (await make_stores(tmp_path))[0]


async def make_plugin_stores(tmp_path: Path) -> tuple[PluginStore, PluginSecretStore]:
    """A plugin store and its secret store on one database."""
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(tmp_path / "keyfile.json")
    await vault.setup("pw")
    store = PluginStore(engine, vault, RevisionStore(engine, vault))
    return store, PluginSecretStore(engine, vault, store)


async def make_stores(tmp_path: Path) -> tuple[PluginStore, SkillStore]:
    """A plugin store and a skill store on one database."""
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(tmp_path / "keyfile.json")
    await vault.setup("pw")
    return PluginStore(engine, vault, RevisionStore(engine, vault)), SkillStore(engine, vault)
