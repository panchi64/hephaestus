"""The notice that raises a task list left open when the model is about to stop."""

from __future__ import annotations

import hashlib

from core.container import ServiceContainer
from services.subagents import SubagentLauncher
from services.task_list import ConversationTasks


async def open_tasks_notice(
    caps: ServiceContainer, owner_id: str, conversation_id: str | None
) -> str | None:
    """The notice text for the conversation's open steps, once per distinct open set."""
    tasks = caps.get_optional(ConversationTasks)
    if tasks is None or conversation_id is None:
        return None
    still_open = await tasks.open_tasks(owner_id, conversation_id)
    if not still_open:
        return None
    # Ending the turn is how an agent waits on the agents it spawned, and their reports wake
    # it. Leave the open set unmarked so the stop after the last report still raises it.
    launcher = caps.get_optional(SubagentLauncher)
    if launcher is not None and await launcher.awaiting_reports(owner_id, conversation_id):
        return None
    # Over the steps' text, not their ids: a model that rewrites the same list gets new ids.
    fingerprint = hashlib.sha256(
        "\n".join(sorted(item.content.strip() for item in still_open)).encode()
    ).hexdigest()
    if fingerprint == await tasks.reminded_open(owner_id, conversation_id):
        return None
    await tasks.mark_reminded(owner_id, conversation_id, fingerprint)
    steps = "\n".join(f"- {item.content} ({item.status.value})" for item in still_open)
    return (
        f"Your task list still has open steps:\n{steps}\n"
        + "Finish them, mark them cancelled, or say why they stay open."
    )
