"""Chat attachments — a file goes to the agent's computer, not into its context.

A document the operator attaches is **staged into the conversation's sandbox** (``/work``)
under ``attachments/`` and announced to the model as a short marker: filename, upload id,
mime, size, and the path it lives at. The model then decides what to read and pages
through the file itself with its files/code tools. Nothing of the file's *text* enters the
prompt — a 300-page PDF costs the same handful of marker tokens as a one-line note, and
the model reads only the parts it actually needs.

Two shapes still come back, and the split now matters for images only:

- :attr:`ResolvedAttachments.content` — what the live attach turn sees.
- :attr:`ResolvedAttachments.persisted` — what replayed history carries.

An **image** is handed to a vision model as pixels and retained inline in both (there is
nothing to "sift through" in a picture, and no way to re-see one on demand), so for images
the two differ from a document only in kind. Its bytes are staged too, so code can crop or
convert it without a round-trip. For a **document** the two shapes are identical: the
marker, and only the marker.

**Degrade path.** The sandbox is fail-closed and can be unavailable (no runtime, a locked
vault, a workspace that won't open). Pointing the model at a path that doesn't exist would
be worse than a big prompt, so when staging fails the file's extracted text is handed over
**inline and in full**, and the marker says exactly that instead of naming a path.

**Staleness is announced, not hidden.** Sandbox sessions are per-conversation and
recyclable: a thread replayed days later may find nothing at the staged path. The marker
tells the model so, and names ``attachments_provision`` (re-stage by id, same path) and
``corpus_retrieve`` (semantic search over the text) as the two ways back.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from pydantic_ai import BinaryContent
from pydantic_ai.messages import UserContent

from core.untrusted import wrap_untrusted
from models.upload import UploadStatus
from services.conversation_view import ATTACHMENTS_MARKER_OPEN
from services.sandbox import stage_attachment
from services.uploads import UploadStore, UploadView
from services.workspace import RunWorkspace

logger = logging.getLogger(__name__)

# Only true images get handed to a vision model as pixels; every other file is read from
# the sandbox by the agent itself, which is both cheaper and better than any amount of
# extracted text we could pour into the prompt.
_IMAGE_PREFIX = "image/"


@dataclass(frozen=True)
class ResolvedAttachments:
    """The two faces of a turn's attachments. ``content`` is what the live user prompt
    carries; ``persisted`` is what replaces it in durable/replayed history. They are
    identical for documents (a marker naming the staged path) and differ only in that an
    image's pixels ride in both. ``ids`` are the uploads that actually resolved
    (foreign/deleted ids dropped), so only real attachments get stamped as chips."""

    content: list[UserContent]  # live-turn
    persisted: list[UserContent]  # durable
    ids: list[str]


@dataclass(frozen=True)
class _Staged:
    """One resolved attachment's line in the marker block, plus whether its text had to
    ride inline because staging failed."""

    filename: str
    upload_id: str
    mime: str
    size_bytes: int
    path: str | None  # None ⇒ not staged (see `inline_text`)


async def resolve_attachments(
    uploads: UploadStore,
    owner_id: str,
    ids: list[str],
    *,
    vision: bool,
    workspace: RunWorkspace | None = None,
) -> ResolvedAttachments:
    """Resolve attached upload ids into the live and durable content for this turn.

    Every non-image upload's **original bytes** are staged into the run's ``workspace``
    (the conversation's sandbox, or a worktree thread's worktree — see
    ``services/workspace.py``); an image's are staged too, and it additionally goes in as
    ``BinaryContent`` when the model can see (``vision``). Both returned shapes then carry
    one short marker block naming each file and its staged path — no extracted text.

    When the sandbox is unavailable, or staging a particular file fails, that file
    degrades to its extracted text inline (wrapped untrusted — file content is data, never
    instructions) and the marker says so rather than naming a path that isn't there. A
    file still being extracted contributes a short placeholder. Unknown or foreign ids are
    skipped."""
    content: list[UserContent] = []
    persisted: list[UserContent] = []
    staged: list[_Staged] = []
    resolved_ids: list[str] = []
    views = await uploads.get_many(owner_id, ids)
    present = [i for i in ids if i in views]
    # One batched read for the whole turn. With a workspace to stage into, every file's
    # bytes are needed; without one, only the images a vision model will actually see.
    wanted = (
        present
        if workspace is not None
        else [i for i in present if views[i].mime.startswith(_IMAGE_PREFIX)]
        if vision
        else []
    )
    blobs = await uploads.contents(owner_id, wanted) if wanted else {}
    for upload_id in present:
        view = views[upload_id]
        resolved_ids.append(upload_id)
        blob = blobs.get(upload_id)
        path = _stage(workspace, upload_id, view.filename, blob.content if blob else None)
        staged.append(
            _Staged(
                filename=view.filename,
                upload_id=upload_id,
                mime=view.mime,
                size_bytes=view.size_bytes,
                path=path,
            )
        )
        if vision and view.mime.startswith(_IMAGE_PREFIX) and blob is not None:
            # An image is pixels in both shapes — bounded in cost, and unre-fetchable
            # once the turn is behind us.
            binary = BinaryContent(data=blob.content, media_type=view.mime)
            content.append(binary)
            persisted.append(binary)
        elif path is None:
            # Not staged — the file's own content has to ride inline or the model has
            # nothing at all. Identical in both shapes: there is no cap to apply.
            inline = _inline_fallback(view)
            content.append(inline)
            persisted.append(inline)
    marker = _marker(staged, workspace)
    if marker:
        content.append(marker)
        persisted.append(marker)
    return ResolvedAttachments(content=content, persisted=persisted, ids=resolved_ids)


def _stage(
    workspace: RunWorkspace | None, upload_id: str, filename: str, content: bytes | None
) -> str | None:
    """Stage one file's original bytes and return the path the model should be told, or
    ``None`` when there was no workspace, no bytes (still extracting), or the write
    failed."""
    if workspace is None or content is None:
        return None
    try:
        relpath, _ = stage_attachment(
            workspace.files,
            filename=filename,
            upload_id=upload_id,
            content=content,
            prefix=workspace.stage_prefix,
        )
    except Exception:
        # Deliberately not just `SandboxError`. Staging writes the host-side workspace
        # directory, so a full disk, a permission problem, or a failed restore of a sealed
        # workspace surfaces as an `OSError` (or worse) rather than a domain error — and a
        # chat turn must degrade to inline text there, exactly as it does for an unavailable
        # sandbox, not die before the model is ever called.
        logger.info("attachments: could not stage %s", upload_id, exc_info=True)
        return None
    return workspace.display(relpath)


def _inline_fallback(view: UploadView) -> str:
    """What a file contributes when it could not be staged: its extracted text in full
    (wrapped untrusted), or a short placeholder while extraction is still running."""
    if view.status == UploadStatus.DONE and view.extracted_text:
        return wrap_untrusted(view.extracted_text, source=view.filename)
    return wrap_untrusted(
        f"[{view.filename} is still being processed — its text isn't available yet.]",
        source=view.filename,
    )


def _marker(staged: list[_Staged], workspace: RunWorkspace | None) -> str:
    """The trusted block closing the attachment set — one line per file (name, id, mime,
    size, and where it lives) plus how to act on it.

    Kept *outside* the untrusted wrapper: it is the chassis talking to the model about
    where the operator's files are, not file content. It names the staleness recovery
    explicitly because a replayed thread genuinely can find nothing at these paths."""
    if not staged:
        return ""
    lines = [_line(item) for item in staged]
    body = "\n".join(lines)
    # A worktree run's files sit in a git worktree that is not recycled and is not the
    # model's own scratch space, so the recycling warning would be untrue there — and
    # telling it these are throwaway paths in a directory it is about to commit from is
    # exactly the wrong picture.
    where = (
        "These paths live in this project's working tree, in a directory git ignores."
        if workspace is not None and workspace.kind == "worktree"
        else "These paths live on your computer, which is recycled between sessions."
    )
    guidance = (
        "Read a file from its path with the files_* tools — page through a large one "
        f"rather than reading it whole. {where} If a read fails because the file is not "
        "there, re-stage it with attachments_provision using the id above (it comes back "
        "at the same path). To search a document's text semantically instead of reading "
        "it, use corpus_retrieve with its id."
    )
    return f"{ATTACHMENTS_MARKER_OPEN}\n{body}\n\n{guidance}]"


def _line(item: _Staged) -> str:
    """One file's line in the marker block."""
    head = f"- {item.filename} (id: {item.upload_id}, {item.mime}, {item.size_bytes:,} bytes)"
    if item.path is None:
        return f"{head} — could not be staged on your computer; its content is inline above."
    return f"{head} → {item.path}"
