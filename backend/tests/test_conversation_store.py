"""The conversation store's denormalized last-used model.

The model a conversation "last ran on" is held on the ``Conversation`` row, kept
in step with the active leaf, so the listing reads it without opening a message
blob and a warm read and a cold read agree — even after branching moves the
active path onto an answer from a different model.
"""

from __future__ import annotations

from pydantic_ai import ModelMessage, ModelRequest, ModelResponse, TextPart, UserPromptPart

from routes.deps import OPERATOR_ID
from services.conversations import ConversationStore

from ._helpers import app_state, client_app


def _turn(prompt: str, answer: str, model: str) -> list[ModelMessage]:
    """A user request + the assistant answer that a given model produced."""
    return [
        ModelRequest(parts=[UserPromptPart(content=prompt)]),
        ModelResponse(parts=[TextPart(content=answer)], model_name=model),
    ]


async def _model_after_cold_read(store: ConversationStore, conversation_id: str) -> str | None:
    """The summary's model with no warm tree to fall back on — the durable value."""
    await store._worker.join()
    store._cache.clear()
    summary = await store.get_summary(conversation_id, OPERATOR_ID)
    return summary.model


async def test_last_used_model_tracks_the_active_branch_across_a_version_switch():
    async with client_app() as (_client, app):
        store = app_state(app, "conversations", ConversationStore)
        cid = await store.create_conversation(OPERATOR_ID)

        # First answer ran on model-a.
        _ = store.record(cid, _turn("q", "from a", "model-a"))
        assert await _model_after_cold_read(store, cid) == "model-a"

        # Regenerate that answer onto model-b — the active path now ends on b.
        turn = (await store.messages_view(cid))[1]
        assert await store.regenerate_point(cid, turn.id)
        _ = store.record(
            cid, [ModelResponse(parts=[TextPart(content="from b")], model_name="model-b")]
        )
        assert await _model_after_cold_read(store, cid) == "model-b"

        # Switch the version back to the model-a answer. The cold read must reflect
        # the active branch (a), not the higher-seq abandoned sibling (b).
        turn = (await store.messages_view(cid))[1]
        assert await store.switch_version(cid, turn.id, 0)
        assert await _model_after_cold_read(store, cid) == "model-a"


async def test_ephemeral_conversations_are_hidden_from_the_listing_but_readable():
    """A compare pane's scratch thread is a real conversation — addressable and
    resumable by id — but it must not surface in the list or the count."""
    async with client_app() as (_client, app):
        store = app_state(app, "conversations", ConversationStore)

        normal = await store.create_conversation(OPERATOR_ID)
        scratch = await store.create_conversation(OPERATOR_ID, ephemeral=True)

        listed = {c.id for c in await store.list_conversations(OPERATOR_ID)}
        assert normal in listed
        assert scratch not in listed
        assert await store.count_conversations(OPERATOR_ID) == 1

        # Still a real conversation: owned, addressable, and readable by id.
        assert await store.exists(scratch, OPERATOR_ID)
        assert await store.get_summary(scratch, OPERATOR_ID) is not None
