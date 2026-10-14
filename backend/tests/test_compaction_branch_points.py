"""Compaction at a **branch point** — folding while a regenerate or an edit has the
active leaf reseated.

This used to be refused outright, which made the one moment compaction is most needed —
re-answering the very request that just filled the window — the one moment it could not
happen. It is allowed now, and what makes it safe is a single rule:

    **A compaction checkpoint is transparent in version sets.**

The checkpoint lands under the reseated leaf and the incoming answer hangs off the
checkpoint, so every enumeration that asks "what are this turn's other versions" has to
look *through* the checkpoint rather than stopping at it. These tests pin that rule at
each site it has to hold: version chips, version switching, a second regenerate, rewind,
delete, the turn a later fold's divider anchors on, and a cold reload — because a shape
that only holds in memory is a shape that breaks on the operator's next page load.
"""

from __future__ import annotations

from fastapi import FastAPI
from pydantic_ai import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.usage import RequestUsage

from agent import build_chat_orchestrator
from agent.summarize import build_auto_compact_policy
from core.config import Settings
from routes.deps import OPERATOR_ID
from runs import RunRegistry, RunStatus
from services.conversation_view import MessageView
from services.conversations import ConversationStore

from ._helpers import app_state, client_app, fold_aware_model, run_bodies


def _conversations(app: FastAPI) -> ConversationStore:
    return app_state(app, "conversations", ConversationStore)


def _turn(prompt: str, answer: str, input_tokens: int = 0) -> list[ModelMessage]:
    """One exchange. ``input_tokens`` is what the answer *reports* the request cost, which
    is the figure the engine's threshold reads."""
    return [
        ModelRequest(parts=[UserPromptPart(content=prompt)]),
        ModelResponse(
            parts=[TextPart(content=answer)],
            usage=RequestUsage(input_tokens=input_tokens, output_tokens=10),
        ),
    ]


def _texts(messages: list[ModelMessage]) -> list[str]:
    """Every message's flattened text, for asserting on shape without part surgery."""
    texts: list[str] = []
    for message in messages:
        for part in message.parts:
            if isinstance(part, UserPromptPart | TextPart | SystemPromptPart) and isinstance(
                part.content, str
            ):
                texts.append(part.content)
    return texts


async def _compact(store: ConversationStore, cid: str, *, summary: str = "SUMMARY") -> str | None:
    """Fold with a fixed summary — the tree work is under test here, not the model."""
    plan = await store.compaction_plan(cid)
    if plan is None:
        return None
    return store.record_compaction(
        cid,
        summary=summary,
        through_id=plan.through_id,
        expected_leaf_id=plan.expected_leaf_id,
        reason="threshold",
    )


async def _seed(store: ConversationStore, cid: str, count: int = 4) -> None:
    for i in range(count):
        _ = store.record(cid, _turn(f"q{i}", f"a{i}"))


def _assistant_views(views: list[MessageView]) -> list[MessageView]:
    return [v for v in views if v.role == "assistant"]


async def _fold_at_a_regenerate(store: ConversationStore, cid: str) -> str:
    """Seed four turns, reseat for a regenerate of the last answer, fold, and record the
    replacement answer. Returns the new answer's node id. The shape every test below
    starts from."""
    await _seed(store, cid)
    answer = _assistant_views(await store.messages_view(cid))[-1]
    assert await store.regenerate_point(cid, answer.id)
    assert await _compact(store, cid) is not None
    _ = store.record(cid, [ModelResponse(parts=[TextPart(content="a3-again")])])
    return _assistant_views(await store.messages_view(cid))[-1].id


# --- the fold itself ---------------------------------------------------------


async def test_a_regenerate_at_the_threshold_folds_and_answers_the_same_request():
    """The engine path, end to end: a nearly full window plus a reseated leaf. The turn
    folds, and the replay still *ends* on the request being regenerated — a fold that
    swallowed it would have the model re-answering its own summary."""
    async with client_app() as (_client, app):
        store = _conversations(app)
        cid = await store.create_conversation(OPERATOR_ID)
        _ = store.record(cid, _turn("q0", "a0", 100))
        _ = store.record(cid, _turn("q1", "a1", 100))
        # 96% of a 10k window, reported on the answer *before* the one being regenerated:
        # reseating drops the last answer off the path, and with it the figure it reported.
        _ = store.record(cid, _turn("q2", "a2", 9_600))
        _ = store.record(cid, _turn("q3", "a3", 9_800))

        answer = _assistant_views(await store.messages_view(cid))[-1]
        assert await store.regenerate_point(cid, answer.id)

        orch = build_chat_orchestrator(
            None,  # regenerate: no new prompt, the leaf already sits on the request
            # The thread's own model writes the summary as well as the answer.
            model=fold_aware_model(answer="a better answer", summary="FOLDED AWAY"),
            categories={},
            store=store,
            conversation_id=cid,
            context_window=10_000,
            auto_compact=build_auto_compact_policy(Settings()),
        )
        run = app_state(app, "runs", RunRegistry).submit(
            kind="chat", owner_id=OPERATOR_ID, orchestrator=orch
        )
        await run.wait()
        assert run.status is RunStatus.done

        folds = [b for b in run_bodies(run) if b.type == "conversation.compacted"]
        assert len(folds) == 1

        replayed = _texts(await store.model_history(cid))
        assert replayed[0].endswith("FOLDED AWAY")
        assert replayed[-2:] == ["q3", "a better answer"]

        # The regenerated answer is version 2 of the one it replaced, not an orphan
        # hanging off the checkpoint.
        newest = _assistant_views(await store.messages_view(cid))[-1]
        assert newest.content == "a better answer"
        assert (newest.version_index, newest.version_count) == (1, 2)


async def test_an_edit_at_a_branch_point_versions_the_request_through_the_checkpoint():
    """The same rule from the other side: an edit reseats onto the *response* before the
    request, so the checkpoint lands between them and the edited request is its child."""
    async with client_app() as (_client, app):
        store = _conversations(app)
        cid = await store.create_conversation(OPERATOR_ID)
        await _seed(store, cid)

        original = [v for v in await store.messages_view(cid) if v.role == "user"][-1]
        assert await store.edit_point(cid, original.id)
        assert await _compact(store, cid) is not None
        _ = store.record(cid, _turn("q3-edited", "a3-edited"))

        edited = [v for v in await store.messages_view(cid) if v.role == "user"][-1]
        assert edited.content == "q3-edited"
        assert (edited.version_index, edited.version_count) == (1, 2)
        assert _texts(await store.model_history(cid))[-2:] == ["q3-edited", "a3-edited"]


async def test_the_reseated_request_survives_the_fold():
    """A fold cuts at the end of the path, which at a branch point would swallow the
    request about to be re-answered and leave the regenerate answering the summary. The
    boundary is held one node short instead."""
    async with client_app() as (_client, app):
        store = _conversations(app)
        cid = await store.create_conversation(OPERATOR_ID)
        await _seed(store, cid)

        answer = _assistant_views(await store.messages_view(cid))[-1]
        assert await store.regenerate_point(cid, answer.id)
        assert await _compact(store, cid) is not None

        assert _texts(await store.model_history(cid)) == ["SUMMARY", "q3"]


async def test_a_checkpoint_just_folded_at_a_branch_point_has_nothing_after_it():
    """The request the fold left standing sits *before* the checkpoint on the path, still
    waiting on its answer. Folding again before that answer lands would take it with it,
    so there is nothing to fold until the answer has been recorded."""
    async with client_app() as (_client, app):
        store = _conversations(app)
        cid = await store.create_conversation(OPERATOR_ID)
        await _seed(store, cid)

        answer = _assistant_views(await store.messages_view(cid))[-1]
        assert await store.regenerate_point(cid, answer.id)
        assert await _compact(store, cid) is not None
        assert await store.compaction_plan(cid) is None

        _ = store.record(cid, [ModelResponse(parts=[TextPart(content="a3-again")])])
        plan = await store.compaction_plan(cid)
        assert plan is not None
        assert _texts(plan.messages) == ["SUMMARY", "q3", "a3-again"]


async def test_a_branch_point_fold_on_top_of_an_earlier_one_absorbs_it():
    """The two rules meeting: a second fold reaches back only to the previous checkpoint,
    and this one also has a reseated leaf to graft under."""
    async with client_app() as (_client, app):
        store = _conversations(app)
        cid = await store.create_conversation(OPERATOR_ID)
        await _seed(store, cid)
        assert await _compact(store, cid, summary="FIRST") is not None
        _ = store.record(cid, _turn("q4", "a4"))
        _ = store.record(cid, _turn("q5", "a5"))

        answer = _assistant_views(await store.messages_view(cid))[-1]
        assert await store.regenerate_point(cid, answer.id)
        plan = await store.compaction_plan(cid)
        assert plan is not None
        # The older summary is folded *into* the new one, never re-exposed as a turn, and
        # the request being regenerated is held back.
        assert _texts(plan.messages) == ["FIRST", "q4", "a4"]

        assert await _compact(store, cid, summary="SECOND") is not None
        _ = store.record(cid, [ModelResponse(parts=[TextPart(content="a5-again")])])

        assert _texts(await store.model_history(cid)) == ["SECOND", "q5", "a5-again"]
        newest = _assistant_views(await store.messages_view(cid))[-1]
        assert (newest.version_index, newest.version_count) == (1, 2)


async def test_the_divider_itself_is_not_a_version_of_anything():
    """The checkpoint is a sibling of the answer it was folded beside. It is bookkeeping,
    not an alternative the operator can cycle to, so it carries no version chips."""
    async with client_app() as (_client, app):
        store = _conversations(app)
        cid = await store.create_conversation(OPERATOR_ID)
        _ = await _fold_at_a_regenerate(store, cid)

        divider = next(v for v in await store.messages_view(cid) if v.role == "compaction")
        assert (divider.version_index, divider.version_count) == (0, 1)


# --- navigating across the fold ----------------------------------------------


async def test_switching_versions_reads_through_the_checkpoint():
    """Version 1 was written before the fold and version 2 below it. Cycling between them
    works, and each branch replays whatever fold its own path carries."""
    async with client_app() as (_client, app):
        store = _conversations(app)
        cid = await store.create_conversation(OPERATOR_ID)
        newest_id = await _fold_at_a_regenerate(store, cid)

        assert await store.switch_version(cid, newest_id, 0)
        # The original answer's branch never had a checkpoint on it, so it replays whole.
        assert _texts(await store.model_history(cid)) == [
            "q0",
            "a0",
            "q1",
            "a1",
            "q2",
            "a2",
            "q3",
            "a3",
        ]
        restored = _assistant_views(await store.messages_view(cid))[-1]
        assert restored.content == "a3"
        assert (restored.version_index, restored.version_count) == (0, 2)

        assert await store.switch_version(cid, restored.id, 1)
        assert _texts(await store.model_history(cid))[0] == "SUMMARY"
        assert _texts(await store.model_history(cid))[-1] == "a3-again"


async def test_a_second_regenerate_after_a_fold_joins_the_same_version_set():
    """Regenerating the folded answer reseats onto the checkpoint — deliberately, since
    branching above it would replay the whole unfolded thread, which is the one history
    known not to fit. The third answer still joins the other two."""
    async with client_app() as (_client, app):
        store = _conversations(app)
        cid = await store.create_conversation(OPERATOR_ID)
        newest_id = await _fold_at_a_regenerate(store, cid)

        assert await store.regenerate_point(cid, newest_id)
        _ = store.record(cid, [ModelResponse(parts=[TextPart(content="a3-third")])])

        newest = _assistant_views(await store.messages_view(cid))[-1]
        assert newest.content == "a3-third"
        assert (newest.version_index, newest.version_count) == (2, 3)
        # The fold survived the second branch: the summary is still the replay's premise.
        assert _texts(await store.model_history(cid))[0] == "SUMMARY"


async def test_rewinding_to_the_last_turn_keeps_the_fold_and_above_it_drops_it():
    """A checkpoint at the tip is part of the last turn's tail, not the start of a new
    one. Rewinding to that turn must keep it; rewinding above it restores the full replay,
    which is the documented way back out of a compaction."""
    async with client_app() as (_client, app):
        store = _conversations(app)
        cid = await store.create_conversation(OPERATOR_ID)
        await _seed(store, cid)
        assert await _compact(store, cid) is not None

        views = await store.messages_view(cid)
        last_answer = _assistant_views(views)[-1]
        assert await store.rewind(cid, last_answer.id)
        assert _texts(await store.model_history(cid)) == ["SUMMARY"]

        earlier = _assistant_views(views)[1]  # a1, above the divider
        assert await store.rewind(cid, earlier.id)
        assert _texts(await store.model_history(cid)) == ["q0", "a0", "q1", "a1"]


async def test_deleting_the_branch_under_a_checkpoint_takes_the_checkpoint_with_it():
    """A checkpoint whose only branch is deleted covers nothing any more. Left behind it
    would strand the active leaf on a divider for a turn that is gone, and keep claiming a
    version that no longer exists."""
    async with client_app() as (_client, app):
        store = _conversations(app)
        cid = await store.create_conversation(OPERATOR_ID)
        newest_id = await _fold_at_a_regenerate(store, cid)

        assert await store.delete_message(cid, newest_id)

        views = await store.messages_view(cid)
        # The divider went with the branch it was carrying, so the thread reseats onto the
        # request itself (as any deleted last turn does) rather than onto a stranded fold.
        assert not [v for v in views if v.role == "compaction"]
        assert "SUMMARY" not in _texts(await store.history(cid))
        assert [v.content for v in views if v.role == "user"][-1] == "q3"
        assert _texts(await store.model_history(cid))[-1] == "q3"
        # …and the surviving version is a plain child of that request again, so the next
        # send branches beside it exactly as it would have before the fold.
        assert await store.switch_version(cid, [v.id for v in views if v.role == "user"][-1], 0)
        assert _texts(await store.model_history(cid))[-1] == "a3"


# --- durability --------------------------------------------------------------


async def test_a_cold_reload_projects_the_same_branched_tree():
    """The checkpoint's parent is a branch point, and its flags ride the row. A rehydrated
    tree has to rebuild the same version sets — the warm shape is the one the operator saw
    before they closed the tab."""
    async with client_app() as (_client, app):
        store = _conversations(app)
        cid = await store.create_conversation(OPERATOR_ID)
        _ = await _fold_at_a_regenerate(store, cid)

        warm = [
            (v.role, v.content, v.version_index, v.version_count)
            for v in await store.messages_view(cid)
        ]
        warm_replay = _texts(await store.model_history(cid))

        await store.flush()
        store.evict_cache()

        cold = [
            (v.role, v.content, v.version_index, v.version_count)
            for v in await store.messages_view(cid)
        ]
        assert cold == warm
        assert _texts(await store.model_history(cid)) == warm_replay


# --- turn boundaries across a checkpoint -------------------------------------


async def test_the_first_turn_after_a_checkpoint_anchors_the_next_divider():
    """`_is_turn_start` reads "follows a response" — and a checkpoint is a request. Without
    treating one as a turn boundary, the exchange right after a fold is invisible, and a
    second fold that ends in it would anchor its divider on the last turn *before* the
    first checkpoint — a live client drawing it somewhere a reload would not."""
    async with client_app() as (_client, app):
        store = _conversations(app)
        cid = await store.create_conversation(OPERATOR_ID)
        await _seed(store, cid)
        assert await _compact(store, cid) is not None
        _ = store.record(cid, _turn("q4", "a4"))

        plan = await store.compaction_plan(cid)
        assert plan is not None
        assert plan.anchor_id == _assistant_views(await store.messages_view(cid))[-1].id
