"""Recording a turn: extending the tree off the active leaf and queueing the durable write, plus
stop markers and pins on recorded nodes.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic_ai import ModelMessage, ModelRequest, ModelResponse
from pydantic_ai.messages import UserContent

from runs.timings import ResponseTiming
from services.commands.spec import Invocation
from services.conversation_view import COMMAND_KEY, FILE_REFS_KEY

from .core import _StoreCore
from .paths import _active_path_model
from .projection import _MESSAGE, _project, install_persisted_attachments
from .rows import _PersistJob, _Row
from .tree import _Tree


class _RecordingMixin(_StoreCore):
    def record(
        self,
        conversation_id: str,
        new_messages: list[ModelMessage],
        attachment_ids: list[str] | None = None,
        file_refs: list[str] | None = None,
        command: Invocation | None = None,
        persisted: Sequence[UserContent] | None = None,
        blocked_reason: str | None = None,
        timings: list[ResponseTiming] | None = None,
        prompt_id: str | None = None,
    ) -> list[str]:
        """Hot path: extend the tree off the active leaf and queue the durable write,
        returning the ids of the nodes it created (empty when it wrote nothing).

        Only projects and serializes here (no vault) — the drainer encrypts just
        before the write, on the lock-aware side of the queue. New messages branch
        automatically when a prior regenerate/edit moved the active leaf back.
        ``prompt_id`` says the turn's user request is **already in the tree** — written by
        :meth:`record_prompt` as the turn began — so ``new_messages`` is only what followed
        it. Nothing in it is then stamped as the turn's request (the first request it holds
        is a steering message, not the prompt), and a ``blocked_reason`` with no response to
        carry it lands on the prompt node itself, durably, instead of being dropped with an
        empty slice.
        ``attachment_ids``/``file_refs``/``command``/``persisted`` belong to the turn's user
        request (the first one):
        the ids are stamped on its node + row (chip rendering), and when ``persisted`` is
        given the request's *live* attachment content is replaced by that capped set before
        serialize — so replayed history carries only the retained-up-to-cap content, never
        the uncapped live payload. Installing it lives here (not in the engine) because
        *what the durable blob contains* is the store's job. ``blocked_reason``, when the
        turn ended blocked (a usage/loop/context/time bound), stamps the turn's branch
        node — the first response, matching how ``project_tree`` keys an assistant view —
        so a reload carries the same persistent stop marker the live stream showed.
        ``timings``, when the caller measured them, are the run's per-response wall-clock
        in the order the responses were produced — zipped onto the response nodes below,
        so the readout can total a cold thread's time the way it totals its tokens."""
        if not new_messages:
            if prompt_id is not None and blocked_reason:
                self._block_node(conversation_id, prompt_id, blocked_reason)
            return []
        tree = self._cache_get(conversation_id)
        if tree is None:
            # Nothing cached: either a brand-new conversation (`create_conversation`
            # seeds an empty tree, so normally this doesn't fire) or one whose id no
            # longer exists — a delete that landed while this turn was still running.
            # An empty tree is right for the first case and a resurrected ghost in the
            # second, so `_persist_messages` re-checks the row before writing.
            tree = self._cache_put(conversation_id, _Tree())
        added = tree.append_chain(new_messages)
        # A turn blocked before it produced a response (a time/cancel bound tripping in
        # the pre-model setup window) has no response node to carry the marker — so the
        # turn's user request carries it instead, and a reload still shows the persistent
        # stop marker (under the operator's own message) rather than dropping it.
        blocked_on_request = bool(blocked_reason) and not any(
            isinstance(n.message, ModelResponse) for n in added
        )
        if blocked_on_request and prompt_id is not None:
            self._block_node(conversation_id, prompt_id, blocked_reason or "")
            blocked_on_request = False
            blocked_reason = None
        rows: list[_Row] = []
        # A turn whose request is already recorded has no request left to stamp.
        stamped = prompt_id is not None
        blocked_stamped = False
        # One timing per response, consumed in the order the responses appear — the
        # order the translator produced them in. A short (or absent) list simply leaves
        # the remaining responses unmeasured: a mismatch means the run stopped somewhere
        # the stopwatch didn't reach, and a null there is the honest answer, where
        # misaligning the rest would attribute one response's time to another.
        pending_timings = iter(timings or ())
        for node in added:
            # The attached files belong to this turn's user request (the first one);
            # install the capped content and stamp the ids before we project/serialize.
            if not stamped and getattr(node.message, "kind", "") == "request":
                if attachment_ids:
                    node.attachment_ids = list(attachment_ids)
                # The `@` references ride the request's own ``metadata`` rather than a
                # column of their own — the same place the compaction reason rides, and
                # for the same three reasons: the library carries the field through
                # serialization, never sends it to a provider, and it lands inside the
                # sealed blob every read already opens. A host path is operator data and
                # a column would have to be sealed on its own; nothing scans these the
                # way `referenced_upload_ids` scans the attachment ids, so there is
                # nothing a clear column would buy.
                #
                # The picked command rides the same dict, one key over. What is written
                # there is the *invocation* — the name typed and the argument after it —
                # never the block it expanded to: that block is rebuilt from whatever the
                # name means at the moment it is needed, which is what keeps a regenerate
                # from replaying an old copy of a template that has since been edited.
                stamp = {
                    **({FILE_REFS_KEY: list(file_refs)} if file_refs else {}),
                    **(
                        {COMMAND_KEY: {"name": command.name, "argument": command.argument}}
                        if command is not None
                        else {}
                    ),
                }
                if stamp:
                    node.message.metadata = {
                        **(getattr(node.message, "metadata", None) or {}),
                        **stamp,
                    }
                install_persisted_attachments(node.message, persisted)
                if blocked_on_request:
                    node.blocked_reason = blocked_reason
                stamped = True
            if (
                not blocked_stamped
                and blocked_reason
                and not blocked_on_request
                and isinstance(node.message, ModelResponse)
            ):
                node.blocked_reason = blocked_reason
                blocked_stamped = True
            if isinstance(node.message, ModelResponse):
                timing = next(pending_timings, None)
                if timing is not None:
                    node.llm_ms = timing.llm_ms
                    node.ttft_ms = timing.ttft_ms
                    node.tool_ms = timing.tool_ms
            kind, text = _project(node.message)
            blob = _MESSAGE.dump_json(node.message).decode()
            rows.append(
                _Row(
                    id=node.id,
                    parent_id=node.parent_id,
                    seq=node.seq,
                    kind=kind,
                    text=text,
                    blob=blob,
                    attachment_ids=node.attachment_ids,
                    blocked_reason=node.blocked_reason,
                    llm_ms=node.llm_ms,
                    ttft_ms=node.ttft_ms,
                    tool_ms=node.tool_ms,
                )
            )
        self._submit(
            _PersistJob(
                kind="messages",
                conversation_id=conversation_id,
                active_leaf_id=tree.active_leaf_id,
                rows=rows,
                model=_active_path_model(tree.active_path()),
            )
        )
        return [node.id for node in added]

    def record_prompt(
        self,
        conversation_id: str,
        prompt: ModelRequest,
        *,
        attachment_ids: list[str] | None = None,
        file_refs: list[str] | None = None,
        command: Invocation | None = None,
        persisted: Sequence[UserContent] | None = None,
    ) -> str:
        """Write the operator's prompt down **as the turn begins**, returning its node id.

        A turn used to be recorded whole when it finished, so a thread opened mid-run had
        no prompt for the run in flight and a cancel that captured nothing lost the
        operator's own words. The prompt is the one part of a turn that exists before the
        model is called, so it goes in first — as the new leaf, which makes everything the
        turn produces afterwards a child of it (:meth:`record` with ``prompt_id``). The
        stamps are exactly the ones :meth:`record` puts on a turn's first request."""
        return self.record(
            conversation_id,
            [prompt],
            attachment_ids=attachment_ids,
            file_refs=file_refs,
            command=command,
            persisted=persisted,
        )[0]

    async def leaf_id(self, conversation_id: str) -> str | None:
        """The active leaf — on a regenerate, the request about to be re-answered."""
        return (await self._tree(conversation_id)).active_leaf_id

    def _block_node(self, conversation_id: str, node_id: str, reason: str) -> None:
        """Stamp a stop marker on a node that is already recorded, durably.

        The prompt of a turn that stopped before producing anything is the only place its
        marker can go; it is already in the tree and (probably) in the table, so this
        rewrites the node and queues the matching row update. A node that already carries
        a marker keeps it — the first stop is the one that explains the turn."""
        tree = self._cache.get(conversation_id)
        node = tree.nodes.get(node_id) if tree is not None else None
        if node is None or node.blocked_reason is not None:
            return
        node.blocked_reason = reason
        self._submit(
            _PersistJob(
                kind="block",
                conversation_id=conversation_id,
                message_id=node.id,
                blocked_reason=reason,
            )
        )

    async def set_pin(self, conversation_id: str, message_id: str, pinned: bool) -> bool:
        """Pin or unpin a turn (by its branch node id). Returns False if unknown.
        Queued behind any in-flight message writes so it lands on a persisted row."""
        tree = await self._tree(conversation_id)
        node = tree.nodes.get(message_id)
        if node is None:
            return False
        node.pinned = pinned
        self._submit(
            _PersistJob(
                kind="pin", conversation_id=conversation_id, message_id=message_id, pinned=pinned
            )
        )
        return True

    async def clear_blocked_reason(self, conversation_id: str, message_id: str) -> bool:
        """Retire the persistent stop marker on the turn whose branch node is
        ``message_id``. Returns False if the node is unknown or was never blocked.

        A stop marker says "this turn was cut short and nothing has been done about
        it" — a standing prompt to resume. Once the operator *has* resumed it, the
        marker is answering a question nobody is asking any more, so it is cleared
        rather than left to accumulate down the transcript. Clearing is durable (it
        rewrites the node and its row) because the marker itself is durable: a
        reload must not resurrect a stop the operator already handled. Queued behind
        any in-flight message writes so it lands on a persisted row.

        An unrecognized ``message_id`` falls back to the *newest* blocked turn on the
        active path. A live client names the turn by the id it is showing, and for a
        turn that stopped before anything was persisted that id is the client's own
        optimistic one — never a node id here. Such an id can only ever name the turn
        being streamed, i.e. the newest, so the fallback is unambiguous even with
        older markers still standing further up the thread — honour the intent rather
        than leaving a stop the operator has visibly resolved."""
        tree = await self._tree(conversation_id)
        node = tree.nodes.get(message_id)
        if node is None:
            node = next(
                (n for n in reversed(tree.active_path()) if n.blocked_reason is not None), None
            )
        if node is None or node.blocked_reason is None:
            return False
        node.blocked_reason = None
        self._submit(
            _PersistJob(kind="unblock", conversation_id=conversation_id, message_id=node.id)
        )
        return True
