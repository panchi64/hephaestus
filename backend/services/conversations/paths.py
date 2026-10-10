"""Pure walks over an active path of tree nodes: turn boundaries, the model's replay view
(compaction checkpoint hoisted), the operator's transcript view, and the model a path last ran
on.
"""

from __future__ import annotations

from typing import NamedTuple

from pydantic_ai import ModelMessage, ModelRequest, ModelResponse, UserPromptPart

from .tree import _Node


def _is_user_prompt(message: ModelMessage) -> bool:
    """A request that carries an operator prompt — the boundary that starts a new
    turn (tool-return requests don't)."""
    return isinstance(message, ModelRequest) and any(
        isinstance(p, UserPromptPart) for p in message.parts
    )


def _opens_turn(node: _Node) -> bool:
    """Whether this node *could* open an operator turn: it carries a user prompt and is
    not a compaction checkpoint. A checkpoint is a ``ModelRequest`` with a user part —
    that is how the model reads a summary — so every walk that counts turns or extends
    one to its tail has to tell the two apart by the flag, never by the shape."""
    return _is_user_prompt(node.message) and not node.compacted


def _awaiting_answer(path: list[_Node]) -> bool:
    """Whether the path ends on an operator prompt nothing has answered yet — a turn that
    has begun, or one that stopped before its first token. A compaction checkpoint is a
    request too, and ends a path after every fold, so the flag is what tells them apart."""
    return bool(path) and _opens_turn(path[-1])


def _is_turn_start(path: list[_Node], index: int) -> bool:
    """Whether ``path[index]`` opens a fresh operator turn — a user-prompt request that is
    the conversation's root, directly follows an assistant response, or directly follows a
    compaction checkpoint.

    Not every user-prompt request opens a turn. A message the operator sends *while a run
    is executing* is persisted as its own request sitting directly behind the tool-return
    request it was injected into (the engine's injected-request split), so counting bare
    user prompts would read a mid-run aside as a whole exchange.

    A checkpoint counts as a predecessor because the first turn after a fold is a real
    turn: reading only "follows a response" would skip it, and a later fold that ends in
    that turn would anchor its divider on the turn before the checkpoint."""
    if not _opens_turn(path[index]):
        return False
    if index == 0:
        return True
    previous = path[index - 1]
    return isinstance(previous.message, ModelResponse) or previous.compacted


def _checkpoint_split(path: list[_Node]) -> tuple[int, int]:
    """``(checkpoint, through)`` — the path index of the newest compaction checkpoint and
    the index of the last node its summary covers. ``(-1, -1)`` when the path holds no
    checkpoint. A checkpoint whose covered node is no longer on the path degrades to
    ``through == checkpoint``: the summary still stands, it just covers nothing verbatim."""
    checkpoint = next((i for i in range(len(path) - 1, -1, -1) if path[i].compacted), -1)
    if checkpoint < 0:
        return -1, -1
    through_id = path[checkpoint].compacted_through
    through = next((i for i, node in enumerate(path) if node.id == through_id), checkpoint)
    return checkpoint, through


def _replay_nodes(path: list[_Node], stop: int | None = None) -> list[_Node]:
    """The nodes the **model** replays: the newest checkpoint's summary **hoisted to the
    front**, then every node after the one it covers. ``stop`` truncates at a path index,
    which is how a fresh compaction collects exactly what it is about to fold.

    Hoisting is what lets the checkpoint be a plain leaf node in the tree. Splicing it in
    at the boundary instead would mean re-parenting a live node, which would pull the
    turns below it out of their own version sets. Hoisting also preserves the regenerate
    shape: with the leaf reseated onto a user request, the replay still *ends* on that
    request even though the checkpoint was appended after it."""
    end = len(path) if stop is None else stop
    checkpoint, through = _checkpoint_split(path)
    if checkpoint < 0:
        return path[:end]
    head = [path[checkpoint]] if checkpoint < end else []
    return head + [path[i] for i in range(through + 1, end) if i != checkpoint]


def _replay_boundary(path: list[_Node]) -> int:
    """How many nodes at the head of :func:`_replay_nodes` predate the newest checkpoint —
    the stored counterpart of ``Run.fold_boundary``.

    The checkpoint itself and every verbatim node recorded before it are counted: they were
    sized against the history the fold folded away, so a response among them reported a
    prompt that no longer exists. Only what was recorded *after* the checkpoint — the first
    request that actually saw the folded thread — may be believed. ``0`` for a thread that
    never folded."""
    checkpoint, through = _checkpoint_split(path)
    if checkpoint < 0:
        return 0
    return 1 + max(0, checkpoint - through - 1)


class ReplayHistory(NamedTuple):
    """The history the next request carries, and where the part that was measured against
    it begins.

    ``boundary`` is ``Run.fold_boundary`` read off the stored tree: messages before it
    predate the newest fold, so a provider-reported footprint may be read only from
    ``messages[boundary:]``, while an estimate covers all of ``messages``."""

    messages: list[ModelMessage]
    boundary: int


def _view_nodes(path: list[_Node]) -> list[_Node]:
    """The nodes the **operator** sees, in transcript order: each compaction checkpoint
    moved back to sit immediately after the node its summary covers.

    The opposite reordering to :func:`_replay_nodes`, and deliberately so. The model wants
    the summary first, as a preamble; the operator wants it where it happened, so the
    divider reads "everything above this line is what got folded" rather than trailing the
    recent turns it did *not* fold. A checkpoint whose covered node has left the path stays
    where it is rather than disappearing."""
    followers: dict[str, list[_Node]] = {}
    for node in path:
        if node.compacted and node.compacted_through:
            followers.setdefault(node.compacted_through, []).append(node)
    if not followers:
        return path
    on_path = {node.id for node in path}
    out: list[_Node] = []
    for node in path:
        if node.compacted and node.compacted_through in on_path:
            continue  # emitted after its anchor below
        out.append(node)
        out.extend(followers.get(node.id, ()))
    return out


def _turn_anchor_id(path: list[_Node], node_id: str) -> str | None:
    """The id of the **rendered turn** ``node_id`` belongs to — a turn's branch node, the
    same one ``project_tree`` keys a view by: the user request for an operator turn, the
    first response for an assistant one.

    A tree node is not a rendered message: an assistant turn spans several nodes and shows
    as one bubble. A live client is told where to draw the divider in terms it can actually
    address, so the position it draws matches the one a reload computes rather than
    approximating it."""
    index = next((i for i, node in enumerate(path) if node.id == node_id), None)
    if index is None:
        return None
    if _is_user_prompt(path[index].message):
        return path[index].id
    start = max((j for j in range(index + 1) if _is_turn_start(path, j)), default=None)
    if start is None:
        return path[index].id
    first_response = next(
        (path[j].id for j in range(start, index + 1) if isinstance(path[j].message, ModelResponse)),
        None,
    )
    return first_response or path[index].id


def _model_of(message: ModelMessage) -> str | None:
    """The model that produced a response message, or None for a request."""
    return getattr(message, "model_name", None) if isinstance(message, ModelResponse) else None


def _active_path_model(path: list[_Node]) -> str | None:
    """The model the active path last ran on — its most recent response's
    ``model_name``. None for a path with no answer yet."""
    return next((m for m in (_model_of(n.message) for n in reversed(path)) if m), None)
