"""Shared setup for the agent tests: a stored-agent service on an in-memory database."""

from __future__ import annotations

from pathlib import Path

from core.db import init_db, make_engine
from core.vault import Vault
from services.agents import AgentStore
from services.conversations import ConversationStore

OWNER = "operator"


async def make_stores(tmp_path: Path) -> tuple[AgentStore, ConversationStore]:
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(tmp_path / "keyfile.json")
    await vault.setup("pw")
    return AgentStore(engine, vault), ConversationStore(engine, vault)


def agent_values(name: str = "helper", **more: object) -> dict[str, object]:
    return {
        "name": name,
        "description": "Helps with things.",
        "instructions": "Be helpful.",
        **more,
    }
