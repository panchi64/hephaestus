"""Deleting conversations: the thread and everything every other feature keeps for it."""

from __future__ import annotations

from harness.manifest import FeatureManifest, FeatureRuntime, HarnessContext
from harness.manifests._inbox_dispatch import InboxDispatcher
from runs import RunRegistry
from services.browser import BrowserSessionManager
from services.conversations import ConversationStore
from services.conversations.teardown import ConversationTeardown
from services.egress import EgressPolicy
from services.home.changes import ThreadChanges
from services.notifications import NotificationService
from services.plan_mode import PlanMode
from services.projects import ProjectStore, WorktreeManager
from services.sandbox import SandboxSessionManager
from services.subagent_store import SubagentStore
from services.task_list import ConversationTasks
from services.uploads import UploadStore
from services.workspace_history import WorkspaceHistoryStore


async def _build(ctx: HarnessContext) -> FeatureRuntime:
    services = ctx.services
    teardown = ConversationTeardown(
        store=services.get(ConversationStore),
        registry=services.get(RunRegistry),
        uploads=services.get(UploadStore),
        notifications=services.get(NotificationService),
        workspace_history=services.get(WorkspaceHistoryStore),
        conversation_tasks=services.get(ConversationTasks),
        plan_mode=services.get(PlanMode),
        projects=services.get(ProjectStore),
        worktrees=services.get(WorktreeManager),
        egress=services.get(EgressPolicy),
        subagent_records=services.get_optional(SubagentStore),
        changes=services.get_optional(ThreadChanges),
        inbox_dispatcher=services.get_optional(InboxDispatcher),
        sandbox=services.get_optional(SandboxSessionManager),
        browser=services.get_optional(BrowserSessionManager),
    )
    return FeatureRuntime(services=(teardown,), state={"conversation_teardown": teardown})


MANIFEST = FeatureManifest(
    name="conversation-teardown",
    after=("browser", "inbox", "notifications", "projects", "subagents", "uploads", "views"),
    build=_build,
)
