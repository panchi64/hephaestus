"""The one seam through which a tool may move a chat into a worktree.

Handed the conversation store so the agent's capability bag never carries the whole store.
"""

from __future__ import annotations

from services.conversations import ConversationStore


class WorktreeSwitch:
    def __init__(self, conversations: ConversationStore) -> None:
        self._conversations: ConversationStore = conversations

    async def open(self, conversation_id: str) -> bool:
        """Record that ``conversation_id`` works in its project's worktree from now on."""
        return await self._conversations.open_worktree(conversation_id)
