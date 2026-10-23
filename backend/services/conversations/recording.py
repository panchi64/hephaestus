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

from .core import StoreCore
from .paths import active_path_model
from .projection import MESSAGE, install_persisted_attachments, project
from .rows import PersistJob, Row


class RecordingMixin(StoreCore):
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

        Only projects and serializes here; the drainer encrypts before the write. An evicted
        thread is reloaded first, blocking on the read and raising
        :class:`ConversationUnavailableError` when the rows cannot be opened. New messages
        branch automatically when a regenerate/edit moved the active leaf back.

        ``prompt_id`` says the turn's user request is already in the tree
        (:meth:`record_prompt`), so ``new_messages`` is only what followed it: nothing in it
        is stamped as the request, and a ``blocked_reason`` with no response to carry it
        lands on the prompt node.

        ``attachment_ids``/``file_refs``/``command``/``persisted`` belong to the turn's first
        user request. The ids are stamped on its node and row (chips); ``persisted``, when
        given, replaces the request's live attachment content with the capped set before
        serialization, so replayed history never carries the uncapped payload. That lives
        here because what the durable blob contains is the store's job.

        ``blocked_reason``, when the turn ended blocked (a usage, loop, context or time
        bound), stamps the first response, the node ``project_tree`` keys the view by, so a
        reload shows the live stop marker. ``timings`` are per-response wall-clock in
        production order, zipped onto the response nodes so a cold thread's time can be
        totalled like its tokens."""
        if not new_messages:
            if prompt_id is not None and blocked_reason:
                self.stamp_block(conversation_id, prompt_id, blocked_reason)
            return []
        tree = self.cache_get(conversation_id)
        if tree is None:
            # Evicted, or deleted mid-turn (`_persist_messages` re-checks the row).
            tree = self.hydrate_blocking(conversation_id)
        added = tree.append_chain(new_messages)
        # A turn blocked before any response (a bound tripping in the pre-model setup)
        # has no response node, so its user request carries the stop marker.
        blocked_on_request = bool(blocked_reason) and not any(
            isinstance(n.message, ModelResponse) for n in added
        )
        if blocked_on_request and prompt_id is not None:
            self.stamp_block(conversation_id, prompt_id, blocked_reason or "")
            blocked_on_request = False
            blocked_reason = None
        rows: list[Row] = []
        # A turn whose request is already recorded has no request left to stamp.
        stamped = prompt_id is not None
        blocked_stamped = False
        # One timing per response, in production order. A short list leaves the rest null
        # rather than misattributing one response's time to another.
        pending_timings = iter(timings or ())
        for node in added:
            # Attachments belong to the turn's first user request: stamp before serializing.
            if not stamped and getattr(node.message, "kind", "") == "request":
                if attachment_ids:
                    node.attachment_ids = list(attachment_ids)
                # `@` references and the picked command ride the request's ``metadata``:
                # the library serializes it, never sends it to a provider, and it lands in
                # the sealed blob, whereas a column would need sealing of its own (host
                # paths are operator data). The command is stored as its invocation, never
                # its expansion, so a regenerate re-expands the current template.
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
            kind, text = project(node.message)
            blob = MESSAGE.dump_json(node.message).decode()
            rows.append(
                Row(
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
            PersistJob(
                kind="messages",
                conversation_id=conversation_id,
                active_leaf_id=tree.active_leaf_id,
                rows=rows,
                model=active_path_model(tree.active_path()),
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

        Recording it first means a thread opened mid-run shows the prompt and a cancel
        cannot lose it. It becomes the new leaf, so the turn's output is its child
        (:meth:`record` with ``prompt_id``). Stamps match those :meth:`record` applies."""
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
        return (await self.tree(conversation_id)).active_leaf_id

    def stamp_block(self, conversation_id: str, node_id: str, reason: str) -> None:
        """Stamp a stop marker on a node that is already recorded, durably.

        The prompt of a turn that stopped before producing anything is the only place its
        marker can go. A node that already has one keeps it: the first stop explains the
        turn."""
        tree = self._cache.get(conversation_id)
        if tree is None:
            tree = self.hydrate_blocking(conversation_id)
        node = tree.nodes.get(node_id)
        if node is None or node.blocked_reason is not None:
            return
        node.blocked_reason = reason
        self._submit(
            PersistJob(
                kind="block",
                conversation_id=conversation_id,
                message_id=node.id,
                blocked_reason=reason,
            )
        )

    async def set_pin(self, conversation_id: str, message_id: str, pinned: bool) -> bool:
        """Pin or unpin a turn (by its branch node id). Returns False if unknown.
        Queued behind any in-flight message writes so it lands on a persisted row."""
        tree = await self.tree(conversation_id)
        node = tree.nodes.get(message_id)
        if node is None:
            return False
        node.pinned = pinned
        self._submit(
            PersistJob(
                kind="pin", conversation_id=conversation_id, message_id=message_id, pinned=pinned
            )
        )
        return True

    async def clear_blocked_reason(self, conversation_id: str, message_id: str) -> bool:
        """Retire the persistent stop marker on the turn whose branch node is
        ``message_id``. Returns False if the node is unknown or was never blocked.

        A stop marker invites a resume; once the operator has resumed, it is cleared so it
        does not accumulate down the transcript. Durable, like the marker itself, and queued
        behind in-flight message writes so it lands on a persisted row.

        An unrecognized ``message_id`` falls back to the newest blocked turn on the active
        path: for a turn that stopped before anything was persisted, the client's id is its
        own optimistic one, and it can only name the turn being streamed, the newest."""
        tree = await self.tree(conversation_id)
        node = tree.nodes.get(message_id)
        if node is None:
            node = next(
                (n for n in reversed(tree.active_path()) if n.blocked_reason is not None), None
            )
        if node is None or node.blocked_reason is None:
            return False
        node.blocked_reason = None
        self._submit(
            PersistJob(kind="unblock", conversation_id=conversation_id, message_id=node.id)
        )
        return True
