"""Tree navigation: regenerate, edit, rewind, version switching, forking and deleting a turn."""

from __future__ import annotations

from models._fields import new_id

from .core import StoreCore
from .paths import active_path_model, is_user_prompt, opens_turn, turn_start_index
from .projection import MESSAGE, project
from .rows import PersistJob, Row
from .tree import Node, Tree

#: How long a forked thread's title may run before it is cut. The source's title is
#: already a short summary, so this only bites on a hand-typed rename.
_FORK_TITLE_MAX = 80


def _forked_title(source: str | None) -> str | None:
    """The new thread's name: prefixed with the source's, with no model call to title it."""
    if not source or not source.strip():
        return None
    trimmed = source.strip()[:_FORK_TITLE_MAX]
    return f"Fork of {trimmed}"


class NavigationMixin(StoreCore):
    # Each navigation moves the active leaf in memory (authoritative) and queues the durable
    # update. Regenerate/edit only reposition the leaf; the caller's next turn records the
    # new branch.

    def _move_leaf(self, conversation_id: str, leaf_id: str | None) -> None:
        """Queue an active-leaf move; the denormalized model rides along because the new
        path may have run on a different one."""
        tree = self._cache.get(conversation_id)
        model = active_path_model(tree.active_path()) if tree is not None else None
        self._submit(
            PersistJob(
                kind="active_leaf",
                conversation_id=conversation_id,
                active_leaf_id=leaf_id,
                model=model,
            )
        )

    async def _reseat_to_parent(
        self, conversation_id: str, message_id: str, *, require_parent: bool
    ) -> bool:
        """Move the active leaf to ``message_id``'s parent, so the next turn branches
        in as a sibling of ``message_id``. ``require_parent`` rejects a root node
        (regenerate needs a preceding request; edit allows branching from the root).
        Returns False if the node is unknown (or rootless when required).

        A parent that is a compaction checkpoint is kept: branching under it carries the
        fold into the new version, whereas reseating above it would replay the unfolded
        thread, which no longer fits. :meth:`Tree.siblings` looks through checkpoints, so
        the version set is the same either way."""
        tree = await self.tree(conversation_id)
        node = tree.nodes.get(message_id)
        if node is None or (require_parent and node.parent_id is None):
            return False
        tree.active_leaf_id = node.parent_id
        self._move_leaf(conversation_id, tree.active_leaf_id)
        return True

    async def is_reseat_point(
        self, conversation_id: str, message_id: str, *, regenerate: bool
    ) -> bool:
        """Whether :meth:`regenerate_point` (``regenerate``) or :meth:`edit_point` would
        accept ``message_id``, without moving the leaf."""
        node = (await self.tree(conversation_id)).nodes.get(message_id)
        return node is not None and not (regenerate and node.parent_id is None)

    async def regenerate_point(self, conversation_id: str, message_id: str) -> bool:
        """Set up a regenerate of the assistant turn whose branch node is
        ``message_id``: move the active leaf back to the user request that preceded
        it, so a fresh turn (run with no new prompt) records a sibling answer.
        Returns False if the node is unknown or has no preceding request."""
        return await self._reseat_to_parent(conversation_id, message_id, require_parent=True)

    async def edit_point(self, conversation_id: str, message_id: str) -> bool:
        """Set up an edit of the user turn whose request node is ``message_id``:
        move the active leaf to that request's parent, so a fresh turn (run with the
        edited prompt) records a sibling request + answer. Returns False if unknown."""
        return await self._reseat_to_parent(conversation_id, message_id, require_parent=False)

    async def switch_version(
        self, conversation_id: str, message_id: str, target_index: int
    ) -> bool:
        """Cycle the turn at ``message_id`` to version ``target_index`` among its
        siblings, descending that branch to its leaf. Returns False on a bad id or
        out-of-range index; a compaction checkpoint has no version set, so naming one is a
        bad id. Versions can sit on either side of a fold, so the set is enumerated through
        checkpoints and each branch replays the fold its own path carries."""
        tree = await self.tree(conversation_id)
        if message_id not in tree.nodes:
            return False
        siblings = tree.siblings(message_id)
        if not 0 <= target_index < len(siblings):
            return False
        tree.active_leaf_id = tree.descend_to_leaf(siblings[target_index])
        self._move_leaf(conversation_id, tree.active_leaf_id)
        return True

    async def fork(self, conversation_id: str, message_id: str, owner_id: str) -> str | None:
        """Copy this thread's history up to ``message_id`` into a **new** conversation.

        Unlike regenerate/edit/rewind, which move the leaf within one tree, a fork is an
        independent conversation. Returns its id, or None when ``message_id`` isn't on the
        active path.

        Not copied:

        - **runs and pins.** A run belongs to the turn that produced it; a pin is a
          bookmark on the original.
        - **the workspace's contents.** The transcript may name sandbox paths the fork
          lacks, as with any replayed thread (``attachments_provision`` is the way back).
          The caller handles a **coding** fork by branching from the source conversation's
          branch rather than the project's base ref.
        """
        tree = await self.tree(conversation_id)
        path = tree.active_path()
        ids = [n.id for n in path]
        if message_id not in ids:
            return None
        idx = ids.index(message_id)
        # Same tail-of-turn rule as `rewind`: forking from an assistant turn keeps its tool
        # exchanges.
        if not is_user_prompt(path[idx].message):
            while idx + 1 < len(path) and not is_user_prompt(path[idx + 1].message):
                idx += 1
        copied = path[: idx + 1]

        source = await self.get_summary(conversation_id, owner_id)
        binding = await self.binding(conversation_id)
        new_id_ = await self.create_conversation(
            owner_id,
            title=_forked_title(source.title if source is not None else None),
            project_id=binding.project_id,
            # A fork of a thread dropped to Manual must not come back at the default.
            permission=binding.permission,
            agent_name=binding.agent_name,
        )

        forked = self.cache_get(new_id_) or self._cache_put(new_id_, Tree())
        rows: list[Row] = []
        parent: str | None = None
        for seq, node in enumerate(copied):
            # `compacted_through` is not copied: it names a node id in the source tree and
            # would dangle for `model_history`. The checkpoint then reads as covering
            # everything before it, which is true.
            fresh = Node(
                id=new_id(),
                parent_id=parent,
                seq=seq,
                message=node.message,
                # Not `pinned`: a bookmark belongs to the thread it was placed in.
                attachment_ids=list(node.attachment_ids),
                blocked_reason=node.blocked_reason,
                compacted=node.compacted,
            )
            forked.add(fresh)
            kind, text = project(fresh.message)
            rows.append(
                Row(
                    id=fresh.id,
                    parent_id=fresh.parent_id,
                    seq=fresh.seq,
                    kind=kind,
                    text=text,
                    blob=MESSAGE.dump_json(fresh.message).decode(),
                    attachment_ids=fresh.attachment_ids,
                    blocked_reason=fresh.blocked_reason,
                    compacted=fresh.compacted,
                )
            )
            parent = fresh.id
        forked.active_leaf_id = parent
        self._submit(
            PersistJob(
                kind="messages",
                conversation_id=new_id_,
                active_leaf_id=parent,
                rows=rows,
                model=active_path_model(forked.active_path()),
            )
        )
        return new_id_

    async def rewind(self, conversation_id: str, message_id: str) -> bool:
        """Rewind the active leaf to the tail of the turn whose branch node is
        ``message_id`` (on the current path), so the thread ends there and the next
        send branches. Returns False if the node isn't on the active path."""
        tree = await self.tree(conversation_id)
        path = tree.active_path()
        ids = [n.id for n in path]
        if message_id not in ids:
            return False
        idx = ids.index(message_id)
        # Extend to the tail of this turn: an assistant turn spans its response and
        # interleaved tool-return requests up to the next user prompt; a user turn is just
        # its own request. A checkpoint at the tail belongs to it, or the newest fold would
        # drop off the path and restore the full replay.
        target = path[idx]
        if not target.compacted and not is_user_prompt(target.message):
            while idx + 1 < len(path) and not opens_turn(path[idx + 1]):
                idx += 1
        tree.active_leaf_id = path[idx].id
        self._move_leaf(conversation_id, tree.active_leaf_id)
        return True

    async def turn_prompt_ids(self, conversation_id: str, message_id: str) -> list[str] | None:
        """The operator prompts on the active path from the turn holding ``message_id``
        through the leaf, oldest first; None when the node isn't on the active path.

        A request, its tool exchanges and its answer are one turn, so any of their ids names
        it. A mid-run aside is a prompt of its own and is included: a regenerate of the
        answer after it files its changes under the aside."""
        path = (await self.tree(conversation_id)).active_path()
        ids = [node.id for node in path]
        if message_id not in ids:
            return None
        start = turn_start_index(path, ids.index(message_id))
        if start is None:
            return []
        return [node.id for node in path[start:] if opens_turn(node)]

    async def delete_message(self, conversation_id: str, message_id: str) -> bool:
        """Delete the turn whose branch node is ``message_id`` and everything after
        it on every branch (its subtree), reseating the active leaf on the parent if
        it fell inside. Returns False if the node is unknown.

        A checkpoint the delete empties out goes with it and the reseat skips it, so the
        thread does not end on a divider for a deleted turn."""
        tree = await self.tree(conversation_id)
        node = tree.nodes.get(message_id)
        if node is None:
            return False
        subtree = tree.subtree_ids(message_id)  # highest seq first
        stranded = tree.stranded_checkpoints(node.parent_id, set(subtree))
        doomed = sorted(subtree + stranded, key=lambda i: tree.nodes[i].seq, reverse=True)
        gone = set(doomed)
        new_leaf = tree.active_leaf_id
        if new_leaf is None or new_leaf in gone:
            new_leaf = node.parent_id
            while new_leaf is not None and new_leaf in gone:
                new_leaf = tree.nodes[new_leaf].parent_id
        tree.remove(doomed)
        keep = new_leaf is None or new_leaf in tree.nodes
        tree.active_leaf_id = new_leaf if keep else tree.fallback_leaf()
        self._submit(
            PersistJob(
                kind="delete",
                conversation_id=conversation_id,
                active_leaf_id=tree.active_leaf_id,
                deleted_ids=doomed,
                model=active_path_model(tree.active_path()),
            )
        )
        return True
