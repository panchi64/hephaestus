"""Attachments tool — (re-)stage an attached file into this run's workspace.

Every attachment is already staged eagerly, at the moment it is attached
(``agent/attachments.py``), and the turn's marker names the path. This tool is the
**recovery** path for the case that marker warns about: sandbox sessions are
per-conversation and recyclable, so a replayed thread can find nothing at the path it was
told about. Calling this re-stages the original bytes at the same path. It also serves an
attachment from an older turn the agent wants to compute on now.

*Which* workspace comes from the run's resolver (``tools/workspace.py``), not from the
sandbox directly — a worktree thread stages into its worktree, where its file and shell
tools can actually reach the file.

It **provisions**, never reads: the file's bytes go into the workspace, not through the
model. It complements ``corpus_retrieve`` (semantic text search over a document) with a
"give me the actual file to compute on" path.

Thin like every tool: the upload store decrypts the bytes, ``services.sandbox.staging``
writes them (the same code the eager attach-time path runs, so both land on the same
path for the same file), and this adapter translates a missing capability / bad id into
something the model can act on.
"""

from __future__ import annotations

from pathlib import PurePosixPath

from pydantic_ai import FunctionToolset, ModelRetry, RunContext

from core.exceptions import NotFoundError
from services.projects.worktree import WorktreeBusyError
from services.sandbox import SandboxError, stage_attachment
from services.uploads import UploadStore

from .deps import RunDeps
from .workspace import run_workspace, unavailable


def attachments_toolset() -> FunctionToolset[RunDeps]:
    toolset: FunctionToolset[RunDeps] = FunctionToolset()

    @toolset.tool
    async def provision(ctx: RunContext[RunDeps], attachment_id: str) -> dict:
        """Re-stage a file the operator attached to this conversation, by the upload id
        in its attachment note. Use it when the path that note gave is gone, or to bring
        back an earlier turn's file.

        Returns the ``path`` it was written to — read it there with the ``files_*``
        tools, using that path verbatim, since a name already taken stages this file
        under a different one. To search a document's text instead, call
        ``corpus_retrieve`` with the same id."""
        uploads = ctx.deps.caps.get_optional(UploadStore)
        if uploads is None:
            return {"ok": False, "error": "Attachments are unavailable right now."}
        try:
            workspace = await run_workspace(ctx)
        except WorktreeBusyError as exc:
            return {"ok": False, "error": str(exc)}
        if workspace is None:
            return {"ok": False, "error": unavailable(ctx.deps)}
        try:
            blob = await uploads.content(ctx.deps.owner_id, attachment_id)
        except NotFoundError:
            raise ModelRetry(
                f"No attachment with id {attachment_id!r}. Use an id from the "
                "attachment note for a file in this conversation."
            ) from None

        try:
            staged_relpath, renamed = stage_attachment(
                workspace.files,
                filename=blob.filename,
                upload_id=attachment_id,
                content=blob.content,
                prefix=workspace.stage_prefix,
            )
        except SandboxError as exc:
            return {"ok": False, "error": f"Could not stage the file: {exc}"}
        result = {
            "ok": True,
            "path": workspace.display(staged_relpath),
            "filename": blob.filename,
            "mime": blob.mime,
            "size_bytes": len(blob.content),
        }
        if renamed:
            result["renamed"] = True
            result["note"] = (
                f"Another attachment already used this name — staged as "
                f"{PurePosixPath(staged_relpath).name!r} instead. Use the returned "
                "path verbatim, not the original filename."
            )
        return result

    return toolset
