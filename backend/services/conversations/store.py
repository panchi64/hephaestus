"""The conversation store itself: the shared core plus one mixin per concern."""

from __future__ import annotations

from .attachments import _AttachmentsMixin
from .compaction import _CompactionMixin
from .core import _StoreCore
from .drainer import _DrainerMixin
from .embedding import _EmbeddingMixin
from .history import _HistoryMixin
from .listing import _ListingMixin
from .metadata import _MetadataMixin
from .navigation import _NavigationMixin
from .recording import _RecordingMixin


class ConversationStore(
    _HistoryMixin,
    _RecordingMixin,
    _NavigationMixin,
    _CompactionMixin,
    _ListingMixin,
    _MetadataMixin,
    _AttachmentsMixin,
    _EmbeddingMixin,
    _DrainerMixin,
    _StoreCore,
):
    """A conversation's working tree in memory, written behind to the database.

    The behaviour is split by concern across the mixins; the state they share (the tree
    cache, the write-behind queue, the owner memo) lives on :class:`_StoreCore`."""
