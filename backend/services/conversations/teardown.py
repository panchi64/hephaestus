"""Tearing a conversation down: the thread, and everything that exists only because of it."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from core.exceptions import NotFoundError, UnmergedWorkError
from models.subagent import SubagentRecord
from runs import ConversationBusyError, InboxDispatch, RunRegistry
from services.conversations import ConversationStore
from services.egress import EgressPolicy
from services.home.changes import ThreadChanges
from services.notifications import NotificationService
from services.plan_mode import PlanMode
from services.projects import ProjectStore, WorktreeManager
from services.subagent_store import SubagentStore
from services.task_list import ConversationTasks
from services.uploads import UploadStore
from services.workspace_history import WorkspaceHistoryStore

logger = logging.getLogger(__name__)

#: How long a cancelled sub-agent is given to actually stop before its thread is deleted
#: anyway. Long enough for a cooperative cancel to reach the next step boundary, short
#: enough that a wedged child never holds up a delete the operator is waiting on.
_SUBAGENT_STOP_S = 5.0


class Purger(Protocol):
    """A manager holding per-conversation state on disk (the sandbox, the browser)."""

    async def purge(self, key: str) -> None: ...


@dataclass(frozen=True)
class ClearOutcome:
    deleted: int
    # Threads a live run holds; deleting one would tear the tree out from beneath the run.
    skipped_ids: list[str] = field(default_factory=list[str])


class ConversationTeardown:
    """Deletes conversations with their sub-agent threads, orphaned images, notifications
    and every piece of state kept for them."""

    def __init__(
        self,
        *,
        store: ConversationStore,
        registry: RunRegistry,
        uploads: UploadStore,
        notifications: NotificationService,
        workspace_history: WorkspaceHistoryStore,
        conversation_tasks: ConversationTasks,
        plan_mode: PlanMode,
        projects: ProjectStore,
        worktrees: WorktreeManager,
        egress: EgressPolicy,
        subagent_records: SubagentStore | None,
        changes: ThreadChanges | None,
        inbox_dispatcher: InboxDispatch | None,
        sandbox: Purger | None,
        browser: Purger | None,
    ) -> None:
        self._store: ConversationStore = store
        self._registry: RunRegistry = registry
        self._uploads: UploadStore = uploads
        self._notifications: NotificationService = notifications
        self._workspace_history: WorkspaceHistoryStore = workspace_history
        self._conversation_tasks: ConversationTasks = conversation_tasks
        self._plan_mode: PlanMode = plan_mode
        self._projects: ProjectStore = projects
        self._worktrees: WorktreeManager = worktrees
        self._egress: EgressPolicy = egress
        self._subagent_records: SubagentStore | None = subagent_records
        self._changes: ThreadChanges | None = changes
        self._inbox_dispatcher: InboxDispatch | None = inbox_dispatcher
        self.sandbox: Purger | None = sandbox
        self.browser: Purger | None = browser

    async def delete(
        self,
        owner_id: str,
        conversation_id: str,
        *,
        discard_branch: bool,
        purge_images: bool = True,
    ) -> set[str]:
        """Delete a conversation and its sub-agent threads, with their notifications and
        everything kept for them. Returns the upload ids they had attached.

        With ``purge_images`` the images nothing else references are purged too; a bulk
        delete passes False and purges once at the end.

        Raises ``NotFoundError`` when it doesn't exist, ``ConversationBusyError`` when a
        live run holds it (deleting under a run tears the tree out from beneath it), and
        ``UnmergedWorkError`` for a worktree thread with unmerged commits unless
        ``discard_branch``.
        """
        self._registry.claim(conversation_id, owner_id)
        try:
            if await self._store.get_summary(conversation_id, owner_id) is None:
                raise NotFoundError("conversation not found")
            await self._settle_code_branch(owner_id, conversation_id, discard=discard_branch)
            subagents = await self._subagents(owner_id, conversation_id)
            thread_ids = {conversation_id, *(row.child_conversation_id for row in subagents)}
            attached = await self._store.attachment_ids(owner_id, thread_ids)
            await self._store.delete_conversation(conversation_id)
            # Queued messages, View history, the task list, the plan and the originals of
            # changed project files all restate the operator's content; none outlives it.
            self._registry.inbox(conversation_id, owner_id).clear()
            await self._workspace_history.delete_for_conversation(owner_id, conversation_id)
            await self._conversation_tasks.delete_for_conversation(owner_id, conversation_id)
            await self._plan_mode.delete_for_conversation(owner_id, conversation_id)
            if self._changes is not None:
                await self._changes.delete_for_conversation(owner_id, conversation_id)
            await self._discard_subagents(conversation_id, subagents)
            _ = await self._notifications.delete_for_conversations(owner_id, thread_ids)
            if purge_images:
                await self.purge_unreferenced_images(owner_id, attached)
            await self._purge_disk_state(conversation_id)
            return attached
        finally:
            self._registry.release_and_kick(conversation_id, owner_id, self._inbox_dispatcher)

    async def clear_all(self, owner_id: str) -> ClearOutcome:
        """Delete every conversation the owner has, discarding unmerged branches. Threads
        a live run holds are skipped, along with the sub-agent threads under them."""
        summaries = await self._store.list_conversations(owner_id)
        parents = {summary.id: summary.parent_id for summary in summaries}

        def root(conversation_id: str) -> str:
            while (parent := parents.get(conversation_id)) is not None and parent in parents:
                conversation_id = parent
            return conversation_id

        attached: set[str] = set()
        deleted = 0
        skipped: set[str] = set()
        try:
            # Top-level threads first: each takes its sub-agent threads with it.
            for summary in summaries:
                if summary.parent_id is not None:
                    continue
                if await self._delete_for_clear(owner_id, summary.id, attached):
                    deleted += 1
                else:
                    skipped.add(summary.id)
            # What is left hangs under a skipped thread, whose live run still uses it, or
            # under a parent that is already gone.
            for summary in summaries:
                if summary.parent_id is not None and root(summary.id) not in skipped:
                    _ = await self._delete_for_clear(owner_id, summary.id, attached)
        finally:
            await self.purge_unreferenced_images(owner_id, attached)
        return ClearOutcome(deleted=deleted, skipped_ids=sorted(skipped))

    async def message_image_orphans(
        self, owner_id: str, conversation_id: str, message_id: str
    ) -> list[str]:
        """The image uploads deleting this turn's subtree would orphan — computed before the
        delete runs, while the doomed turns are still in the tree."""
        candidates = await self._store.orphaned_attachments_for_delete(
            owner_id, conversation_id, message_id=message_id
        )
        return await self._uploads.image_ids(owner_id, candidates)

    async def purge_unreferenced_images(self, owner_id: str, upload_ids: set[str]) -> None:
        """Purge the images among ``upload_ids`` that no surviving message references. Run
        after the deletes, so the deleted threads no longer count as references."""
        referenced = await self._store.referenced_upload_ids(owner_id)
        candidates = sorted(upload_ids - referenced)
        await self.purge_uploads(owner_id, await self._uploads.image_ids(owner_id, candidates))

    async def purge_uploads(self, owner_id: str, upload_ids: list[str]) -> None:
        """Hard-delete these image uploads, after the conversation or message is gone.

        Best-effort per id: one already gone or failing to delete is logged and skipped, so
        it never aborts the rest or fails a delete the operator already saw succeed."""
        for upload_id in upload_ids:
            try:
                await self._uploads.delete(owner_id, upload_id)
            except NotFoundError:
                pass
            except Exception:  # one bad purge mustn't strand the others
                logger.exception("failed to purge orphaned image upload %s", upload_id)

    async def _delete_for_clear(
        self, owner_id: str, conversation_id: str, attached: set[str]
    ) -> bool:
        """False when a live run holds the thread; True when it is gone, including when it
        already went with its parent."""
        try:
            attached |= await self.delete(
                owner_id, conversation_id, discard_branch=True, purge_images=False
            )
        except ConversationBusyError:
            return False
        except NotFoundError:
            pass
        return True

    async def _purge_disk_state(self, conversation_id: str) -> None:
        """The sandbox, egress allowlist and browser profile (its saved logins). Best-effort:
        the DB delete is the authoritative action."""
        for name, purger in (("sandbox", self.sandbox), ("browser", self.browser)):
            if purger is None:
                continue
            try:
                await purger.purge(conversation_id)
            except Exception:  # best-effort; the DB delete already succeeded
                logger.warning("%s purge failed for %s", name, conversation_id, exc_info=True)
        try:
            await self._egress.forget(conversation_id)
        except Exception:  # best-effort; the DB delete already succeeded
            logger.warning("egress purge failed for %s", conversation_id, exc_info=True)

    async def _subagents(self, owner_id: str, conversation_id: str) -> list[SubagentRecord]:
        """Every agent below a thread, at any depth; empty when sub-agents aren't wired."""
        if self._subagent_records is None:
            return []
        try:
            return await self._subagent_records.descendants(conversation_id, owner_id)
        except Exception:  # a delete must not fail on a listing it can live without
            logger.warning("could not list sub-agents of %s", conversation_id, exc_info=True)
            return []

    async def _discard_subagents(self, conversation_id: str, rows: list[SubagentRecord]) -> None:
        """Stop and forget these agents below a thread, because the thread is going.

        A sub-agent is a live run: one whose parent is gone keeps making model requests and
        counting against the cap. Its hidden thread goes too, since the deleted cards were
        the only way in. Best-effort per sub-agent, because the conversation row is gone.
        """
        if self._subagent_records is None or not rows:
            return
        for row in rows:
            try:
                _ = await self._registry.cancel(row.run_id)
                run = self._registry.get(row.run_id)
                if run is not None:
                    # Before its thread is deleted: a run finalising into a gone
                    # conversation re-creates a cache entry nothing evicts. Bounded so a
                    # wedged child never leaves the operator's delete hanging.
                    await asyncio.wait_for(run.wait(), timeout=_SUBAGENT_STOP_S)
            except Exception:  # one stuck child mustn't strand its siblings
                logger.warning("could not stop sub-agent %s", row.id, exc_info=True)
        for row in rows:
            try:
                await self._store.delete_conversation(row.child_conversation_id)
            except Exception:  # one stuck child mustn't strand its siblings
                logger.warning("could not discard sub-agent %s", row.id, exc_info=True)
        try:
            await self._subagent_records.forget([row.id for row in rows])
        except Exception:  # best-effort; the DB delete already succeeded
            logger.warning("could not forget sub-agents of %s", conversation_id, exc_info=True)

    async def _settle_code_branch(
        self, owner_id: str, conversation_id: str, *, discard: bool
    ) -> None:
        """Refuse to delete a worktree thread holding unmerged commits, or throw its branch
        away when ``discard`` is set.

        Only a real, countable diff blocks: a deleted project or a moved repository leaves
        nothing to protect.
        """
        binding = await self._store.binding(conversation_id)
        if not binding.project_id:
            return
        if not binding.worktree:
            await self._discard_child_worktrees(owner_id, binding.project_id, conversation_id)
            return
        try:
            project = await self._projects.get(owner_id, binding.project_id)
            root = Path(project.root_path)
            diff = await self._worktrees.diff(
                root,
                base_ref=project.base_ref,
                conversation_id=conversation_id,
                project_id=project.id,
            )
        except Exception:  # no branch, no project, no repo: nothing to lose
            logger.debug("no code branch to settle for %s", conversation_id, exc_info=True)
            return
        if diff.files_changed and not discard:
            raise UnmergedWorkError(
                f"This conversation has unmerged work on {diff.branch}: "
                + f"{diff.files_changed} file(s), +{diff.insertions} −{diff.deletions}. "
                + "Merge it first, or delete with discardBranch=true to throw it away."
            )
        await self._worktrees.discard(
            root, project_id=binding.project_id, conversation_id=conversation_id
        )

    async def _discard_child_worktrees(
        self, owner_id: str, project_id: str, conversation_id: str
    ) -> None:
        """Remove the worktrees of the sub-agents a deleted thread spawned into them."""
        try:
            project = await self._projects.get(owner_id, project_id)
        except NotFoundError:
            return
        await self._worktrees.discard_children(
            Path(project.root_path), project_id=project_id, conversation_id=conversation_id
        )
