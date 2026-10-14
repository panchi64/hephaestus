"""Reading a conversation back: the full transcript, the model's replay view, timings, turn stamps
and the rendered turns.
"""

from __future__ import annotations

from pydantic_ai import ModelMessage, ModelRequest

from runs.timings import TimingTotals
from services.conversation_view import MessageView, project_tree, stamped_command, stamped_file_refs

from .core import StoreCore
from .paths import ReplayHistory, replay_boundary, replay_nodes, view_nodes
from .records import TurnStamps


class HistoryMixin(StoreCore):
    async def history(self, conversation_id: str) -> list[ModelMessage]:
        """The active path's messages — the full transcript, compaction and all.

        This is what the operator's own surfaces read (titling, retitling, the counts and
        timings of the readout). The **model's** view is :meth:`model_history`, which is
        narrower once a thread has been compacted, and the context meter measures that
        view through :meth:`replay_history` — a window sized over the transcript would sit
        far above what the next request actually carries. The two are deliberately separate
        methods rather than one with a flag, because every caller here wants the whole
        thread."""
        tree = await self.tree(conversation_id)
        return [node.message for node in tree.active_path()]

    async def replay_history(self, conversation_id: str) -> ReplayHistory:
        """What the next request replays, with the fold boundary the live gauge would use.

        The same nodes as :meth:`model_history`, plus the index a live turn keeps on
        ``Run.fold_boundary`` — so a cold read measures the context exactly as the stream
        did: the footprint from responses recorded since the newest fold, and an estimate
        over the whole replay when none has landed yet."""
        tree = await self.tree(conversation_id)
        path = tree.active_path()
        return ReplayHistory([node.message for node in replay_nodes(path)], replay_boundary(path))

    async def timings(self, conversation_id: str) -> TimingTotals:
        """The active path's wall-clock, summed — the one figure in the readout that
        can't be recovered from the messages themselves.

        Walks the same active path :meth:`history` does, so time follows a rewind or a
        version switch exactly as the token counts beside it do. Responses recorded
        before timings existed (or by a run whose stopwatch never reached them) carry
        null and contribute nothing, which is why a thread can honestly report tokens
        it can't report a duration for."""
        tree = await self.tree(conversation_id)
        nodes = tree.active_path()
        ttft = [n.ttft_ms for n in nodes if n.ttft_ms is not None]
        return TimingTotals(
            llm_ms=sum(n.llm_ms or 0 for n in nodes),
            tool_ms=sum(n.tool_ms or 0 for n in nodes),
            ttft_ms_total=sum(ttft),
            ttft_samples=len(ttft),
        )

    async def model_history(self, conversation_id: str) -> list[ModelMessage]:
        """The history the **model** replays: identical to :meth:`history` until the
        thread has been compacted, then the newest checkpoint's summary followed by the
        turns it doesn't cover. Nothing is lost — the folded turns stay in the tree and in
        the transcript; only what is re-sent each turn shrinks."""
        tree = await self.tree(conversation_id)
        return [node.message for node in replay_nodes(tree.active_path())]

    async def turn_stamps(self, conversation_id: str, message_id: str | None = None) -> TurnStamps:
        """What a turn was sent with besides its text — the slash command, the `@` paths.

        Two callers, both of them history-rewriting turns that have to reproduce a turn
        they did not receive:

        * **regenerate** passes no ``message_id`` and reads the trailing request on the
          active path. It calls this *after* ``regenerate_point``, which is what puts the
          turn being re-answered at the leaf.
        * **edit** names the request being replaced, and must read *before* ``edit_point``,
          which reseats the leaf to that request's **parent** — after it, the trailing
          request is the previous turn and the stamps would belong to the wrong message.

        The no-id form walks backwards to the last request rather than taking ``path[-1]``,
        so that on a settled thread it still answers about the most recent turn rather than
        about whichever node happens to sit at the end.

        Cheap on purpose — the cached tree, and no decrypt beyond the one ``tree`` already
        did — because it runs on every regenerate and every edit, including the
        overwhelming majority that carried neither.
        """
        tree = await self.tree(conversation_id)
        if message_id is not None:
            node = tree.nodes.get(message_id)
            message = node.message if node is not None else None
        else:
            message = next(
                (
                    n.message
                    for n in reversed(tree.active_path())
                    if isinstance(n.message, ModelRequest)
                ),
                None,
            )
        if not isinstance(message, ModelRequest):
            return TurnStamps()
        return TurnStamps(command=stamped_command(message), file_refs=stamped_file_refs(message))

    async def messages_view(self, conversation_id: str) -> list[MessageView]:
        """The active path projected to render-ready user/assistant turns (reasoning
        split out, tool calls stitched to results), each carrying its branch node id
        and version index/count so the operator can regenerate, edit, or cycle it."""
        tree = await self.tree(conversation_id)
        nodes = view_nodes(tree.active_path())
        compacted_ids = frozenset(n.id for n in nodes if n.compacted)
        views = project_tree([(n.id, n.message) for n in nodes], compacted_ids=compacted_ids)
        for view in views:
            node = tree.nodes.get(view.id)
            if node is not None:
                view.pinned = node.pinned
                view.attachment_ids = node.attachment_ids
                view.blocked_reason = node.blocked_reason
            siblings = tree.siblings(view.id)
            if siblings:
                view.version_count = len(siblings)
                view.version_index = siblings.index(view.id) if view.id in siblings else 0
        return views

    async def leaf_id(self, conversation_id: str) -> str | None:
        """The active leaf — on a regenerate, the request about to be re-answered."""
        return (await self.tree(conversation_id)).active_leaf_id
