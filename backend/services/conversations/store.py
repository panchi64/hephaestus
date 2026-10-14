"""The conversation store itself: the shared core plus one mixin per concern."""

from __future__ import annotations

from .attachments import AttachmentsMixin
from .compaction import CompactionMixin
from .core import StoreCore
from .drainer import DrainerMixin
from .embedding import EmbeddingMixin
from .history import HistoryMixin
from .listing import ListingMixin
from .metadata import MetadataMixin
from .navigation import NavigationMixin
from .recording import RecordingMixin


class ConversationStore(
    HistoryMixin,
    RecordingMixin,
    NavigationMixin,
    CompactionMixin,
    ListingMixin,
    MetadataMixin,
    AttachmentsMixin,
    EmbeddingMixin,
    DrainerMixin,
    StoreCore,
):
    """A conversation's working tree in memory, written behind to the database.

    The behaviour is split by concern across the mixins; the state they share (the tree
    cache, the write-behind queue, the owner memo) lives on :class:`StoreCore`."""
