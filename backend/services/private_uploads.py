"""Which uploads belong to the Private area.

A file is private while every chat that attaches it is private. Uploads are deduplicated
per account, so a file sent in an ordinary chat becomes ordinary. A file no chat attaches
keeps the flag it was uploaded with, so one staged in a private composer stays private until
a recorded send says otherwise.

The flags follow recorded messages: the engine settles a turn's files once it records the
prompt, and filing, deleting and teardown settle the files they touch.
"""

from __future__ import annotations

from collections.abc import Iterable

from services.conversations import ConversationStore
from services.uploads import UploadStore


class UploadPrivacy:
    def __init__(self, uploads: UploadStore, conversations: ConversationStore) -> None:
        self._uploads: UploadStore = uploads
        self._conversations: ConversationStore = conversations

    async def reconcile(self, owner_id: str, upload_ids: Iterable[str]) -> None:
        """Recompute these uploads' Private flags from the chats that attach them."""
        privacy = await self._conversations.attachment_privacy(owner_id, upload_ids)
        await self._uploads.set_private(
            owner_id, {uid: flag for uid, flag in privacy.items() if flag is not None}
        )

    async def reconcile_conversations(self, owner_id: str, conversation_ids: Iterable[str]) -> None:
        """Recompute the flags of every upload attached in these conversations."""
        ids = await self._conversations.attachment_ids(owner_id, conversation_ids)
        await self.reconcile(owner_id, ids)
