"""Conversation compaction: planning what a fold covers and recording the checkpoint it leaves."""

from __future__ import annotations

from collections.abc import Sequence

from pydantic_ai import ModelRequest, ToolAvailabilityDeltaPart, UserPromptPart
from pydantic_ai.messages import ModelRequestPart

from runs.events import CompactionReason
from services.conversation_view import COMPACTION_REASON_KEY

from .core import StoreCore
from .paths import (
    active_path_model,
    awaiting_answer,
    checkpoint_split,
    replay_nodes,
    turn_anchor_id,
)
from .projection import MESSAGE
from .records import CompactionPlan
from .rows import PersistJob, Row


class CompactionMixin(StoreCore):
    async def compaction_plan(self, conversation_id: str) -> CompactionPlan | None:
        """What compacting this conversation right now would fold, or ``None`` when it
        would fold nothing.

        A fold takes **everything the model replays** — the newest checkpoint's summary,
        if there is one, and every node after it — and nothing stays verbatim beneath the
        new summary: the summary alone carries the thread from here. Folding the newest
        checkpoint in with the rest is what keeps a compaction from reaching back past an
        earlier one and re-exposing its summary as an ordinary turn; the old summary is
        absorbed into the new one instead. Cutting at the end of the path also means the
        split can never strand an assistant tool call from its result.

        The only thread with nothing to fold is one where **nothing follows the newest
        checkpoint** on the active path — a thread that has just been folded, or has no
        messages at all. A single exchange folds: a lone prompt the model answered by
        filling the window is the thread that most needs it.

        A leaf that already has children is a **branch point** — a regenerate or edit has
        reseated the leaf and its run hasn't recorded yet — and it folds like any other.
        The checkpoint lands under the reseated leaf and the incoming answer hangs off the
        checkpoint, which is exactly where a version set that looks *through* checkpoints
        expects it (:meth:`Tree.siblings`). Refusing here instead is what used to make
        the one moment compaction is most needed — a regenerate against a nearly full
        window — the one moment it could not happen.

        At a branch point the boundary is held one node short of the leaf, so the request
        about to be re-answered is never itself folded away: the cut is otherwise
        ``len(path)``, and a regenerate replaying only the summary would be answering the
        fold instead of the operator. The same hold is why a checkpoint just appended at a
        branch point is a tip with nothing after it: the request it left standing is before
        it on the path, and is still waiting on its answer.

        A leaf that is the operator's **prompt with nothing after it** is held the same way,
        because a turn writes its prompt down as it begins: a fold landing mid-turn (the
        overflow recovery) finds the request that is being answered sitting at the tip, and
        folding it into the summary would have the model answer the fold. The checkpoint is
        appended under it and the turn's own messages hang off the checkpoint — the branch-point
        shape exactly."""
        tree = await self.tree(conversation_id)
        leaf = tree.active_leaf_id
        if leaf is None:
            return None
        path = tree.active_path()
        checkpoint, _ = checkpoint_split(path)
        boundary = len(path) - 1 if tree.children.get(leaf) or awaiting_answer(path) else len(path)
        if boundary <= checkpoint + 1:
            return None
        folded = replay_nodes(path, stop=boundary)
        through_id = path[boundary - 1].id
        return CompactionPlan(
            messages=[node.message for node in folded],
            through_id=through_id,
            expected_leaf_id=leaf,
            anchor_id=turn_anchor_id(path, through_id),
        )

    def record_compaction(
        self,
        conversation_id: str,
        *,
        summary: str,
        through_id: str,
        expected_leaf_id: str,
        reason: CompactionReason,
        revealed_tools: Sequence[str] = (),
    ) -> str | None:
        """Append a compaction checkpoint at the tip and queue its durable write, returning
        the new node's id — or ``None`` when the plan went stale.

        ``revealed_tools`` are the tools the folded stretch had shown the model — the
        dormant groups it loaded for itself. The exchange that revealed them is exactly
        what the fold takes away, and the library re-derives the revealed set from the
        replayed messages on every request, so without this a compaction would silently
        take the browser back off a thread mid-task. They ride the checkpoint as a
        ``ToolAvailabilityDeltaPart`` — the library's own shape for "the tools changed
        here", carrying no content and reaching the model as nothing but the reveal — and
        so persist with the summary rather than living only in the turn that folded.

        ``reason`` is stored on the checkpoint's own ``ModelRequest.metadata``, which is
        the one place a fact about the message can ride without a schema column: the
        library carries the field through serialization and never sends it to a provider,
        so it lands inside the sealed blob every read already opens. Without it the reason
        would live only on the run's ``conversation.compacted`` event, and a divider the
        operator watched appear would lose what caused it the moment they reloaded.

        Synchronous on purpose, exactly like :meth:`record`: the staleness re-check and the
        append happen with no ``await`` between them, so under single-threaded asyncio no
        other coroutine can move the leaf in the window. It has to be re-checked at all
        because generating the summary takes seconds, and the route's conversation claim
        blocks *runs* — not a version switch or a rewind.

        The leaf having children is **not** staleness: that is the regenerate/edit branch
        point :meth:`compaction_plan` now plans for, and the checkpoint is appended there
        as a further child. Only the leaf having *moved* invalidates the summary."""
        tree = self._cache.get(conversation_id)
        if tree is None or tree.active_leaf_id != expected_leaf_id:
            return None
        # The delta leads, so the summary stays the message's *last* part: a checkpoint
        # hoisted in front of a turn is merged into it, and the boundary the engine records
        # there counts the turn's own parts from the end.
        parts: list[ModelRequestPart] = []
        if revealed_tools:
            parts.append(ToolAvailabilityDeltaPart(tools_added=list(revealed_tools)))
        parts.append(UserPromptPart(content=summary))
        message = ModelRequest(parts=parts, metadata={COMPACTION_REASON_KEY: reason})
        node = tree.append_chain([message])[0]
        node.compacted = True
        node.compacted_through = through_id
        self._submit(
            PersistJob(
                kind="messages",
                conversation_id=conversation_id,
                active_leaf_id=tree.active_leaf_id,
                rows=[
                    Row(
                        id=node.id,
                        parent_id=node.parent_id,
                        seq=node.seq,
                        kind="request",
                        # An empty projection, deliberately: a checkpoint contributes no
                        # embedding (the drainer only embeds non-empty text), no cross-chat
                        # search hit, and no listing preview. A summary surfacing in search
                        # as if it were the operator's own words is worse than not
                        # surfacing at all. The text lives only in the sealed blob.
                        text="",
                        blob=MESSAGE.dump_json(message).decode(),
                        attachment_ids=[],
                        blocked_reason=None,
                        compacted=True,
                        compacted_through=through_id,
                    )
                ],
                model=active_path_model(tree.active_path()),
            )
        )
        return node.id
