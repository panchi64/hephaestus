"""Pure walks over an active path of tree nodes: turn boundaries, the model's replay view
(compaction checkpoint hoisted), the operator's transcript view, and the model a path last ran
on.
"""

from __future__ import annotations

from typing import NamedTuple

from pydantic_ai import ModelMessage, ModelRequest, ModelResponse, UserPromptPart

from .tree import Node


def is_user_prompt(message: ModelMessage) -> bool:
    """A request that carries an operator prompt — the boundary that starts a new
    turn (tool-return requests don't)."""
    return isinstance(message, ModelRequest) and any(
        isinstance(p, UserPromptPart) for p in message.parts
    )


def opens_turn(node: Node) -> bool:
    """Whether this node could open an operator turn: a user prompt that is not a
    compaction checkpoint. A checkpoint is also a ``ModelRequest`` with a user part, so the
    two differ only by the flag."""
    return is_user_prompt(node.message) and not node.compacted


def awaiting_answer(path: list[Node]) -> bool:
    """Whether the path ends on an operator prompt nothing has answered yet. A checkpoint
    also ends a path after every fold, so the flag tells them apart."""
    return bool(path) and opens_turn(path[-1])


def _is_turn_start(path: list[Node], index: int) -> bool:
    """Whether ``path[index]`` opens a fresh operator turn — a user-prompt request that is
    the conversation's root, directly follows an assistant response, or directly follows a
    compaction checkpoint.

    A message sent while a run executes is persisted as its own request right behind the
    tool-return request it was injected into (the engine's injected-request split), so bare
    user prompts would count a mid-run aside as a whole exchange.

    A checkpoint counts as a predecessor because the first turn after a fold is a real
    turn; otherwise a later fold ending in it would anchor its divider on the turn before
    the checkpoint."""
    if not opens_turn(path[index]):
        return False
    if index == 0:
        return True
    previous = path[index - 1]
    return isinstance(previous.message, ModelResponse) or previous.compacted


def turn_start_index(path: list[Node], index: int) -> int | None:
    """The path index of the operator turn that ``path[index]`` belongs to, or None when
    nothing at or before it opens one."""
    return max((j for j in range(index + 1) if _is_turn_start(path, j)), default=None)


def checkpoint_split(path: list[Node]) -> tuple[int, int]:
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


def replay_nodes(path: list[Node], stop: int | None = None) -> list[Node]:
    """The nodes the **model** replays: the newest checkpoint's summary **hoisted to the
    front**, then every node after the one it covers. ``stop`` truncates at a path index,
    which is how a fresh compaction collects exactly what it is about to fold.

    Hoisting lets the checkpoint be a plain leaf node; splicing it at the boundary would
    re-parent a live node and pull later turns out of their version sets. It also keeps the
    regenerate shape: with the leaf reseated onto a user request, the replay still ends on
    it though the checkpoint was appended after."""
    end = len(path) if stop is None else stop
    checkpoint, through = checkpoint_split(path)
    if checkpoint < 0:
        return path[:end]
    head = [path[checkpoint]] if checkpoint < end else []
    return head + [path[i] for i in range(through + 1, end) if i != checkpoint]


def replay_boundary(path: list[Node]) -> int:
    """How many nodes at the head of :func:`replay_nodes` predate the newest checkpoint —
    the stored counterpart of ``Run.fold_boundary``.

    The checkpoint and every verbatim node recorded before it are counted: they were sized
    against history the fold removed, so a response among them reported a prompt that no
    longer exists. Only what follows the checkpoint may be believed. ``0`` if never folded."""
    checkpoint, through = checkpoint_split(path)
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


def view_nodes(path: list[Node]) -> list[Node]:
    """The nodes the **operator** sees, in transcript order: each compaction checkpoint
    moved back to sit immediately after the node its summary covers.

    The opposite of :func:`replay_nodes`: the model wants the summary first, the operator
    wants the divider where the fold happened. A checkpoint whose covered node has left the
    path stays where it is."""
    followers: dict[str, list[Node]] = {}
    for node in path:
        if node.compacted and node.compacted_through:
            followers.setdefault(node.compacted_through, []).append(node)
    if not followers:
        return path
    on_path = {node.id for node in path}
    out: list[Node] = []
    for node in path:
        if node.compacted and node.compacted_through in on_path:
            continue  # emitted after its anchor below
        out.append(node)
        out.extend(followers.get(node.id, ()))
    return out


def turn_anchor_id(path: list[Node], node_id: str) -> str | None:
    """The id of the rendered turn ``node_id`` belongs to: the branch node ``project_tree``
    keys a view by (the user request, or an assistant turn's first response).

    An assistant turn spans several nodes but shows as one bubble, so a live client is
    given an id it can address and the divider lands where a reload puts it."""
    index = next((i for i, node in enumerate(path) if node.id == node_id), None)
    if index is None:
        return None
    if is_user_prompt(path[index].message):
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


def active_path_model(path: list[Node]) -> str | None:
    """The model the active path last ran on — its most recent response's
    ``model_name``. None for a path with no answer yet."""
    return next((m for m in (_model_of(n.message) for n in reversed(path)) if m), None)
