"""The in-memory message tree: nodes, parent/child links, version sets and the walks over them."""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic_ai import ModelMessage

from models._fields import new_id


@dataclass
class _Node:
    """One message in the tree: a serialized-on-write ``ModelMessage`` plus the
    structural metadata that orders and links it."""

    id: str
    parent_id: str | None
    seq: int
    message: ModelMessage
    pinned: bool = False
    # Upload ids the operator attached to this turn (a user request). Held alongside the
    # message so the conversation detail can render attachment chips without re-reading the
    # row; the message blob itself carries only the marker, never the file content.
    attachment_ids: list[str] = field(default_factory=list)
    # Set on an assistant turn's first response node when the run that produced it
    # ended blocked — the human-readable reason. None otherwise.
    blocked_reason: str | None = None
    # What this response cost in wall-clock (see `models.conversation.Message`). Held on
    # the node so a warm thread reports the same totals as a cold one — the in-memory
    # tree is authoritative until the drainer catches up, so timings that lived only in
    # the queued row would vanish from the readout for the rest of the session.
    llm_ms: int | None = None
    ttft_ms: int | None = None
    tool_ms: int | None = None
    # A conversation-compaction checkpoint (see `record_compaction`): this node's message
    # is a summary, written by the thread's own model, of everything on the path up to and including
    # `compacted_through`. The summarized nodes stay in the tree untouched — only what the
    # *model* replays changes (`model_history`).
    compacted: bool = False
    compacted_through: str | None = None


class _Tree:
    """A conversation's message tree, held in memory while warm. Children lists are
    kept in ``seq`` (creation) order, which is the version order siblings render in."""

    def __init__(self) -> None:
        self.nodes: dict[str, _Node] = {}
        # parent_id -> child ids, seq-ascending. Roots sit under the None key.
        self.children: dict[str | None, list[str]] = {}
        self.active_leaf_id: str | None = None
        self.next_seq: int = 0

    def add(self, node: _Node) -> None:
        """Attach a node. Callers add in ascending ``seq`` (append assigns the next
        seq; rehydration feeds rows pre-sorted), so child lists stay seq-ordered."""
        self.nodes[node.id] = node
        self.children.setdefault(node.parent_id, []).append(node.id)
        self.next_seq = max(self.next_seq, node.seq + 1)

    def fallback_leaf(self) -> str | None:
        """The most-recently-created node, used when the stored active leaf is
        missing or dangling after a cold load."""
        if not self.nodes:
            return None
        return max(self.nodes.values(), key=lambda n: n.seq).id

    def active_path(self) -> list[_Node]:
        """The nodes from root to the active leaf, in order."""
        path: list[_Node] = []
        cur = self.active_leaf_id
        seen: set[str] = set()
        while cur is not None and cur not in seen:
            node = self.nodes.get(cur)
            if node is None:
                break
            seen.add(cur)
            path.append(node)
            cur = node.parent_id
        path.reverse()
        return path

    def append_chain(self, messages: list[ModelMessage]) -> list[_Node]:
        """Add ``messages`` as a chain hanging off the current active leaf, and
        advance the leaf to the chain's tip. New nodes branch automatically when
        the leaf already has children (a regenerate/edit having moved it back)."""
        added: list[_Node] = []
        parent = self.active_leaf_id
        for message in messages:
            node = _Node(id=new_id(), parent_id=parent, seq=self.next_seq, message=message)
            self.add(node)
            parent = node.id
            added.append(node)
        if added:
            self.active_leaf_id = added[-1].id
        return added

    def _version_parent(self, node_id: str) -> str | None:
        """The node whose children hold ``node_id``'s version set — its parent, walked
        up through any compaction checkpoints.

        A checkpoint is **transparent** in version sets. Folding while a regenerate has
        the leaf reseated appends the checkpoint under that leaf and hangs the incoming
        answer off the checkpoint, so the raw parent of the new answer is bookkeeping,
        not the turn it is a version of."""
        node = self.nodes.get(node_id)
        parent = node.parent_id if node is not None else None
        seen: set[str] = set()
        while parent is not None and parent not in seen:
            candidate = self.nodes.get(parent)
            if candidate is None or not candidate.compacted:
                break
            seen.add(parent)
            parent = candidate.parent_id
        return parent

    def siblings(self, node_id: str) -> list[str]:
        """The version set ``node_id`` belongs to, in version (seq) order, including
        ``node_id`` itself — its :meth:`_version_parent`'s children with every checkpoint
        replaced by the real nodes beneath it.

        A checkpoint has no version set of its own: it is one thread's bookkeeping, not
        an alternative the operator can cycle to."""
        node = self.nodes.get(node_id)
        if node is None or node.compacted:
            return []
        return self._version_nodes(self.children.get(self._version_parent(node_id), []))

    def _version_nodes(self, ids: list[str]) -> list[str]:
        """``ids`` with each compaction checkpoint replaced by its own children (applied
        recursively), sorted into seq order — the order versions render in."""
        out: list[str] = []
        stack = list(reversed(ids))
        seen: set[str] = set()
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            node = self.nodes.get(cur)
            if node is None:
                continue
            if node.compacted:
                stack.extend(reversed(self.children.get(cur, [])))
            else:
                out.append(cur)
        out.sort(key=lambda i: self.nodes[i].seq)
        return out

    def stranded_checkpoints(self, start_id: str | None, doomed: set[str]) -> list[str]:
        """The checkpoints above ``start_id`` that ``doomed`` would leave carrying nothing.

        A checkpoint whose last real descendant is being deleted is bookkeeping for a
        branch that no longer exists — nothing replays through it any more, and leaving it
        behind would strand the active leaf on a divider whose turn is gone."""
        out: list[str] = []
        cur = start_id
        seen: set[str] = set()
        while cur is not None and cur not in seen:
            seen.add(cur)
            node = self.nodes.get(cur)
            if node is None or not node.compacted:
                break
            survivors = set(self.children.get(cur, [])) - doomed - set(out)
            if survivors:
                break
            out.append(cur)
            cur = node.parent_id
        return out

    def descend_to_leaf(self, node_id: str) -> str:
        """Follow the most recent child at each step down to a leaf — the tip a
        branch resumes at when its version is selected. Descending *through* a
        checkpoint is what makes a folded branch selectable: the version set names the
        answer under the checkpoint, and resuming it must land below the fold, not
        above it."""
        cur = node_id
        seen: set[str] = set()
        while cur not in seen:
            seen.add(cur)
            kids = self.children.get(cur, [])
            if not kids:
                return cur
            cur = max(kids, key=lambda cid: self.nodes[cid].seq)
        return cur

    def subtree_ids(self, node_id: str) -> list[str]:
        """``node_id`` and all its descendants, highest seq first (children before
        parents — a safe delete order under self-referential foreign keys)."""
        out: list[str] = []
        stack = [node_id]
        while stack:
            cur = stack.pop()
            if cur in self.nodes and cur not in out:
                out.append(cur)
                stack.extend(self.children.get(cur, []))
        out.sort(key=lambda i: self.nodes[i].seq, reverse=True)
        return out

    def remove(self, ids: list[str]) -> None:
        """Drop the given nodes from the tree, detaching them from their parents."""
        doomed = set(ids)
        for node_id in ids:
            node = self.nodes.get(node_id)
            if node is not None and node.parent_id in self.children:
                self.children[node.parent_id] = [
                    c for c in self.children[node.parent_id] if c != node_id
                ]
            self.nodes.pop(node_id, None)
            self.children.pop(node_id, None)
        # Tidy any now-empty child buckets we left behind.
        for parent_id in list(self.children):
            if parent_id in doomed:
                self.children.pop(parent_id, None)
