"""Message projection: the (kind, text) a row is searched by, and the serialized form."""

from __future__ import annotations

from pydantic import TypeAdapter
from pydantic_ai import ModelMessage, ModelRequest, UserPromptPart

from services.conversation_view import operator_content
from services.shapes import as_list

MESSAGE = TypeAdapter(ModelMessage)
_TEXT_PARTS = {"TextPart", "UserPromptPart", "SystemPromptPart"}


def _part_text(content: object) -> str:
    """Searchable text from a message part's content — a bare string, or the string
    items of a multimodal list (a user prompt that retains an attachment inline, where
    content is ``[prompt, BinaryContent | str, …]``), ignoring binary parts."""
    if isinstance(content, str):
        return content
    items = as_list(content)
    if items is not None and not isinstance(content, tuple):
        return " ".join(item for item in items if isinstance(item, str))
    return ""


def project(message: ModelMessage) -> tuple[str, str]:
    """Derive (kind, text) for listing/search from a ModelMessage. Flattens a
    retained-attachment user prompt (list content) so its text still feeds the
    listing preview and the per-message embedding — a string-only check would drop it.
    A user prompt contributes only the operator's own content, never the chassis tail."""
    kind = message.kind
    pieces = (
        _part_text(
            operator_content(message, part)
            if isinstance(message, ModelRequest) and isinstance(part, UserPromptPart)
            else getattr(part, "content", None)
        )
        for part in message.parts
        if type(part).__name__ in _TEXT_PARTS
    )
    return kind, " ".join(p for p in pieces if p)
