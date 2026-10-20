"""Tearing a conversation down: the thread, and everything that exists only because of it."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import HTTPException, Request

from core.exceptions import NotFoundError
from models.subagent import SubagentRecord
from routes import deps
from routes.deps import OPERATOR_ID
from services.home.changes import ThreadChanges

logger = logging.getLogger(__name__)

#: How long a cancelled sub-agent is given to actually stop before its thread is deleted
#: anyway. Long enough for a cooperative cancel to reach the next step boundary, short
#: enough that a wedged child never holds up a delete the operator is waiting on.
_SUBAGENT_STOP_S = 5.0


async def image_orphans(
    request: Request, conversation_id: str, *, message_id: str | None
) -> list[str]:
    """The image uploads the proposed delete would orphan — computed before the delete runs
    (the doomed turns must still be in the tree) and filtered to ``image/*``. The store's
    check spares any image still referenced by a surviving branch or another chat."""
    candidates = await deps.store(request).orphaned_attachments_for_delete(
        OPERATOR_ID, conversation_id, message_id=message_id
    )
    return await deps.uploads(request).image_ids(OPERATOR_ID, candidates)


async def purge_unreferenced_images(request: Request, upload_ids: set[str]) -> None:
    """Purge the images among ``upload_ids`` that no surviving message references. Run after
    the deletes, so the deleted threads no longer count as references."""
    referenced = await deps.store(request).referenced_upload_ids(OPERATOR_ID)
    candidates = sorted(upload_ids - referenced)
    await purge_uploads(request, await deps.uploads(request).image_ids(OPERATOR_ID, candidates))


async def purge_uploads(request: Request, upload_ids: list[str]) -> None:
    """Hard-delete these image uploads, after the conversation or message is already gone.

    Best-effort per id: one already gone or failing to delete is logged and skipped, so it
    never aborts the rest or 500s a delete the operator already saw succeed."""
    uploads = deps.uploads(request)
    for upload_id in upload_ids:
        try:
            await uploads.delete(OPERATOR_ID, upload_id)
        except NotFoundError:
            pass
        except Exception:  # noqa: BLE001 — one bad purge mustn't strand the others
            logger.exception("failed to purge orphaned image upload %s", upload_id)


async def _subagents(request: Request, conversation_id: str) -> list[SubagentRecord]:
    """Every agent below a thread, at any depth; empty when sub-agents aren't wired."""
    records = deps.subagent_records(request)
    if records is None:
        return []
    try:
        return await records.descendants(conversation_id, OPERATOR_ID)
    except Exception:  # noqa: BLE001 — a delete must not fail on a listing it can live without
        logger.warning("could not list sub-agents of %s", conversation_id, exc_info=True)
        return []


async def _discard_subagents(
    request: Request, conversation_id: str, rows: list[SubagentRecord]
) -> None:
    """Stop and forget these agents below a thread, because the thread is going.

    A sub-agent is a live run: one whose parent is gone keeps making model requests and
    counting against the cap. Its hidden thread goes too, since the deleted cards were the
    only way in. Best-effort per sub-agent, because the conversation row is already gone.
    """
    records = deps.subagent_records(request)
    if records is None or not rows:
        return
    registry = deps.registry(request)
    store = deps.store(request)
    for row in rows:
        try:
            _ = await registry.cancel(row.run_id)
            run = registry.get(row.run_id)
            if run is not None:
                # Before its thread is deleted: a run finalising into a gone conversation
                # re-creates a cache entry nothing evicts. Bounded so a wedged child never
                # leaves the operator's delete hanging.
                await asyncio.wait_for(run.wait(), timeout=_SUBAGENT_STOP_S)
        except Exception:  # noqa: BLE001 — one stuck child mustn't strand its siblings
            logger.warning("could not stop sub-agent %s", row.id, exc_info=True)
    for row in rows:
        try:
            await store.delete_conversation(row.child_conversation_id)
        except Exception:  # noqa: BLE001 — one stuck child mustn't strand its siblings
            logger.warning("could not discard sub-agent %s", row.id, exc_info=True)
    try:
        await records.forget([row.id for row in rows])
    except Exception:  # noqa: BLE001 — best-effort; the DB delete already succeeded
        logger.warning("could not forget sub-agents of %s", conversation_id, exc_info=True)


async def _settle_code_branch(request: Request, conversation_id: str, *, discard: bool) -> None:
    """Refuse (409) to delete a worktree thread holding unmerged commits, or throw its branch
    away when ``discard`` is set.

    Only a real, countable diff blocks: a deleted project or a moved repository leaves
    nothing to protect.
    """
    binding = await deps.store(request).binding(conversation_id)
    if not binding.project_id:
        return
    if not binding.worktree:
        await _discard_child_worktrees(request, binding.project_id, conversation_id)
        return
    try:
        project = await deps.projects(request).get(OPERATOR_ID, binding.project_id)
        root = Path(project.root_path)
        diff = await deps.worktrees(request).diff(
            root,
            base_ref=project.base_ref,
            conversation_id=conversation_id,
            project_id=project.id,
        )
    except Exception:  # noqa: BLE001 — no branch, no project, no repo: nothing to lose
        logger.debug("no code branch to settle for %s", conversation_id, exc_info=True)
        return
    if diff.files_changed and not discard:
        raise HTTPException(
            status_code=409,
            detail=(
                f"This conversation has unmerged work on {diff.branch}: "
                f"{diff.files_changed} file(s), +{diff.insertions} −{diff.deletions}. "
                "Merge it first, or delete with discardBranch=true to throw it away."
            ),
        )
    await deps.worktrees(request).discard(
        root, project_id=binding.project_id, conversation_id=conversation_id
    )


async def _discard_child_worktrees(request: Request, project_id: str, conversation_id: str) -> None:
    """Remove the worktrees of the sub-agents a deleted thread spawned into them."""
    try:
        project = await deps.projects(request).get(OPERATOR_ID, project_id)
    except NotFoundError:
        return
    await deps.worktrees(request).discard_children(
        Path(project.root_path), project_id=project_id, conversation_id=conversation_id
    )


async def delete_conversation(
    request: Request, conversation_id: str, *, discard_branch: bool, purge_images: bool = True
) -> set[str]:
    """Delete a conversation and its sub-agent threads, with their notifications and
    everything kept for them. Returns the upload ids they had attached.

    With ``purge_images`` the images nothing else references are purged too; a bulk delete
    passes False and purges once at the end.

    Raises 404 when it doesn't exist, and 409 when a live run holds it (deleting under a run
    tears the tree out from beneath it) or when it is a worktree thread with unmerged
    commits and ``discard_branch`` is off.
    """
    store = deps.store(request)
    deps.claim_conversation(request, conversation_id)
    try:
        if await store.get_summary(conversation_id, OPERATOR_ID) is None:
            raise HTTPException(status_code=404, detail="conversation not found")
        await _settle_code_branch(request, conversation_id, discard=discard_branch)
        subagents = await _subagents(request, conversation_id)
        thread_ids = {conversation_id, *(row.child_conversation_id for row in subagents)}
        attached = await store.attachment_ids(OPERATOR_ID, thread_ids)
        await store.delete_conversation(conversation_id)
        # Queued messages, View history, the task list, the plan and the originals of
        # changed project files all restate the operator's content; none outlives the thread.
        deps.registry(request).inbox(conversation_id, OPERATOR_ID).clear()
        await deps.workspace_history(request).delete_for_conversation(OPERATOR_ID, conversation_id)
        await deps.conversation_tasks(request).delete_for_conversation(OPERATOR_ID, conversation_id)
        await deps.plan_mode(request).delete_for_conversation(OPERATOR_ID, conversation_id)
        changes = deps.capabilities(request).get_optional(ThreadChanges)
        if changes is not None:
            await changes.delete_for_conversation(OPERATOR_ID, conversation_id)
        await _discard_subagents(request, conversation_id, subagents)
        _ = await deps.notifications(request).delete_for_conversations(OPERATOR_ID, thread_ids)
        if purge_images:
            await purge_unreferenced_images(request, attached)
        # The sandbox, egress allowlist and browser profile (its saved logins) are
        # best-effort: the DB delete above is the authoritative action.
        sandbox = deps.sandbox_sessions(request)
        if sandbox is not None:
            try:
                await sandbox.purge(conversation_id)
            except Exception:  # noqa: BLE001 — best-effort; the DB delete already succeeded
                logger.warning("sandbox purge failed for %s", conversation_id, exc_info=True)
        try:
            await deps.egress(request).forget(conversation_id)
        except Exception:  # noqa: BLE001 — best-effort; the DB delete already succeeded
            logger.warning("egress purge failed for %s", conversation_id, exc_info=True)
        browser = deps.browser_sessions(request)
        if browser is not None:
            try:
                await browser.purge(conversation_id)
            except Exception:  # noqa: BLE001 — best-effort; the DB delete already succeeded
                logger.warning("browser purge failed for %s", conversation_id, exc_info=True)
        return attached
    finally:
        deps.release_conversation(request, conversation_id)
