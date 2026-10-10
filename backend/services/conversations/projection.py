"""Message projection: the (kind, text) a row is searched by, the serialized form (`_MESSAGE`), and
the capped attachment content a request persists.
"""

from __future__ import annotations

from pydantic import TypeAdapter
from pydantic_ai import BinaryContent, ModelMessage, ModelRequest, UserPromptPart

_MESSAGE = TypeAdapter(ModelMessage)
_TEXT_PARTS = {"TextPart", "UserPromptPart", "SystemPromptPart"}


def _part_text(content: object) -> str:
    """Searchable text from a message part's content — a bare string, or the string
    items of a multimodal list (a user prompt that retains an attachment inline, where
    content is ``[prompt, BinaryContent | str, …]``), ignoring binary parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(item for item in content if isinstance(item, str))
    return ""


def _project(message: ModelMessage) -> tuple[str, str]:
    """Derive (kind, text) for listing/search from a ModelMessage. Flattens a
    retained-attachment user prompt (list content) so its text still feeds the
    listing preview and the per-message embedding — a string-only check would drop it."""
    kind = getattr(message, "kind", "")
    pieces = (
        _part_text(part.content) for part in message.parts if type(part).__name__ in _TEXT_PARTS
    )
    return kind, " ".join(p for p in pieces if p)


def _prompt_text(content: object) -> str:
    """The operator's typed text from a user-prompt content value. The engine builds an
    attachment turn's content as ``[prompt, *attachment_parts]``, so the leading string is
    the prompt; a bare string is already it. Deliberately *not* a join (unlike ``_project``
    / ``conversation_view.flatten_content``) — a join would also slurp the injected file text
    this strip exists to drop."""
    if isinstance(content, str):
        return content
    if isinstance(content, list) and content and isinstance(content[0], str):
        return content[0]
    return ""


def install_persisted_attachments(message: ModelMessage, persisted: list | None) -> None:
    """Replace a user request's *live* attachment content with the durable ``persisted``
    parts (the engine's capped set: images + under-cap text inline, larger text cut to a
    pointer, a closing id marker), keeping the operator's typed prompt — so replayed
    history carries the capped content, never the uncapped live payload. Collapses to a
    single string when nothing binary survives (a text-only turn persists exactly like a
    plain string), else keeps the multimodal list (a retained image stays inline). Owned
    by the store because *what gets persisted* is the store's concern; ``record`` applies
    it as it serializes. ``None`` (no seam) and a non-request message are no-ops; an
    **empty list is deliberate** — it strips the request back to the typed prompt alone,
    which is how the engine keeps its per-turn appended prompt context (the document
    state) out of the durable history on a turn with no attachments."""
    if persisted is None or not isinstance(message, ModelRequest):
        return
    for part in message.parts:
        if isinstance(part, UserPromptPart):
            prompt = _prompt_text(part.content)
            items: list = ([prompt] if prompt else []) + list(persisted)
            if any(isinstance(item, BinaryContent) for item in items):
                part.content = items
            else:
                part.content = "\n\n".join(item for item in items if isinstance(item, str)).strip()
            return  # one user-prompt part per request carries the attachments
