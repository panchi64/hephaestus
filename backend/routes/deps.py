"""Shared accessors for the singletons on ``app.state``, so every router resolves them alike."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, cast

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket
from pydantic_ai import AbstractToolset
from sqlalchemy import Engine
from starlette.datastructures import State

from core.api_scopes import ScopeTable
from core.auth import SESSION_COOKIE, AuthManager, token_from_headers
from core.config import Settings
from core.container import ServiceContainer
from core.exceptions import DegradedCapabilityError, NotFoundError, PermissionDeniedError
from core.ratelimit import RateLimiter
from core.vault import Vault
from runs import ConversationBusyError, InboxDispatch, Run, RunRegistry
from services.agents import AgentCatalog, AgentStore
from services.api_token_store import ApiTokenStore
from services.approval_grants import ApprovalGrantStore
from services.approval_ledger import ApprovalLedger
from services.artifacts import ArtifactStore
from services.backup import BackupService
from services.browser import BrowserSessionManager
from services.calendar import CalendarService
from services.commands import CommandRegistry
from services.commands.store import CommandStore
from services.conversation_search import ConversationSearch
from services.conversations import ConversationStore
from services.conversations.teardown import ConversationTeardown
from services.corpus import CorpusChunkStore, CorpusIndex, FolderAdapter
from services.corpus.uploads import UploadsAdapter
from services.credential_store import CredentialStore
from services.egress import EgressPolicy
from services.external_tools import ExternalTools
from services.home.scratch import ScratchDirs
from services.integrations import IntegrationService
from services.mail import MailService
from services.mcp import McpRegistry
from services.memory import MemoryStore
from services.notifications import NotificationService
from services.offline import OfflineModeService
from services.plan_mode import PlanMode
from services.plugins import PluginStore
from services.projects import ProjectStore, WorktreeManager, visible_project_ids
from services.projects.tree import worktree_root
from services.registry import ModelRegistry
from services.reindex import EmbeddingReindexer
from services.routines import RoutineStore, ScheduleNaturalLanguage
from services.scheduler import SchedulerService
from services.search import SearchService
from services.searxng import ManagedSearxng
from services.secret_vault import SecretVaultService
from services.settings_store import SettingsStore
from services.skills import SkillStore
from services.skills.platform import PlatformSkills
from services.subagent_store import SubagentStore
from services.task_list import ConversationTasks
from services.tool_policy import CategoryAvailability, effective_disabled_tools
from services.uploads import UploadStore
from services.vm import ConversationMachines, Previews, ServicesMachine
from services.webfetch import BrowserFetcher, ManagedBrowser
from services.work_summaries import WorkSummaryService
from services.workspace_history import WorkspaceHistoryStore

if TYPE_CHECKING:
    from harness.manifest import DormantCategory, FeatureManifest
    from tools import InstructionProvider, PromptContextProvider, RunDeps

logger = logging.getLogger(__name__)

# Single operator: every record is attributed to this owner until a second human exists.
OPERATOR_ID = "operator"


class AppState(State):
    """``app.state`` as ``create_app`` builds it, with each attribute's type.

    The bare ``self.x: T`` declarations below assign nothing: ``State`` keeps values in its
    own dict, and a class-level default would shadow the stored one."""

    def __init__(self) -> None:
        super().__init__(
            {
                "vm": None,
                "subagent_records": None,
                "browser_sessions": None,
                "inbox_dispatcher": None,
            }
        )
        self.runs: RunRegistry
        self.inbox_dispatcher: InboxDispatch | None
        self.capabilities: ServiceContainer
        self.tool_categories: dict[str, AbstractToolset[RunDeps]]
        self.gated_tools: frozenset[str]
        self.category_availability: tuple[CategoryAvailability, ...]
        self.dormant_categories: tuple[DormantCategory, ...]
        self.dormant_summaries: dict[str, str]
        self.instruction_providers: tuple[InstructionProvider, ...]
        self.prompt_context_providers: tuple[PromptContextProvider, ...]
        self.conversations: ConversationStore
        self.conversation_search: ConversationSearch
        self.corpus: CorpusIndex
        self.models: ModelRegistry
        self.memory: MemoryStore
        self.notifications: NotificationService
        self.conversation_tasks: ConversationTasks
        self.plan_mode: PlanMode
        self.approval_grants: ApprovalGrantStore
        self.approval_ledger: ApprovalLedger
        self.egress: EgressPolicy
        self.skills: SkillStore
        self.platform_skills: PlatformSkills
        self.agents: AgentCatalog
        self.agent_store: AgentStore
        self.plugins: PluginStore
        self.commands: CommandRegistry
        self.command_store: CommandStore
        self.uploads: UploadStore
        self.upload_rate_limiter: RateLimiter
        self.embedding_reindexer: EmbeddingReindexer
        self.search: SearchService
        self.fetcher: BrowserFetcher
        self.browser: ManagedBrowser
        self.searxng: ManagedSearxng
        self.offline: OfflineModeService
        self.artifacts: ArtifactStore
        self.workspace_history: WorkspaceHistoryStore
        self.machines: ConversationMachines | None
        self.previews: Previews | None
        self.scratch: ScratchDirs
        self.browser_sessions: BrowserSessionManager | None
        self.projects: ProjectStore
        self.worktrees: WorktreeManager
        self.settings_store: SettingsStore
        self.credentials: CredentialStore
        self.vault: Vault
        self.auth_manager: AuthManager
        self.workspace_db_intact: bool
        self.db_engine: Engine
        self.scheduler: SchedulerService
        self.routines: RoutineStore
        self.schedule_parser: ScheduleNaturalLanguage
        self.mail: MailService
        self.calendar: CalendarService
        self.external: ExternalTools
        self.secret_vault: SecretVaultService
        self.backup: BackupService
        self.api_tokens: ApiTokenStore | None
        self.run_terminal_tasks: set[asyncio.Task[None]]
        self.settings: Settings
        self.api_scope_table: ScopeTable
        self.preview_client: httpx.AsyncClient
        self.web_client: httpx.AsyncClient
        self.feature_manifests: tuple[FeatureManifest, ...]
        self.network_tools: frozenset[str]
        self.vm: ServicesMachine | None
        self.subagent_records: SubagentStore | None
        self.conversation_teardown: ConversationTeardown
        self.discovery_client: httpx.AsyncClient
        self.work_summaries: WorkSummaryService
        self.corpus_uploads: UploadsAdapter
        self.corpus_folder: FolderAdapter
        self.corpus_chunk_store: CorpusChunkStore
        self.task_run_waiters: dict[str, asyncio.Future[Run]]


#: What an accessor resolves app state from; the app itself serves a turn the inbox starts
#: (``routes/chat/queued.py``).
type Source = FastAPI | Request | WebSocket


def app_state(source: Source) -> AppState:
    """The typed ``app.state`` of the app behind a request, socket or the app itself."""
    app = source if isinstance(source, FastAPI) else cast(object, source.app)
    if not isinstance(app, FastAPI):
        raise RuntimeError("request is not served by a FastAPI app")
    state = app.state
    if not isinstance(state, AppState):
        raise RuntimeError("app.state was not built by create_app")
    return state


def vm(request: Request) -> ServicesMachine | None:
    """The services machine, or None when this deployment never built one."""
    return app_state(request).vm


def web_client(request: Request) -> httpx.AsyncClient:
    """The shared outbound client for fetching operator-supplied URLs (SSRF-gated by the caller)."""
    return app_state(request).web_client


def inbox_dispatcher(request: Source) -> InboxDispatch | None:
    """What starts the next turn from an idle thread's inbox, or None when not wired."""
    return app_state(request).inbox_dispatcher


def conversation_teardown(request: Request) -> ConversationTeardown:
    return app_state(request).conversation_teardown


def subagent_records(request: Request) -> SubagentStore | None:
    """The sub-agent register, or None when sub-agents are not wired in this deployment."""
    return app_state(request).subagent_records


def registry(request: Source) -> RunRegistry:
    return app_state(request).runs


def capabilities(request: Source) -> ServiceContainer:
    """The app's one agent-facing capability bag (`RunDeps.caps`), assembled from every
    manifest's `capabilities` export. Every run path hands the engine this same bag, so the
    capability set can't diverge between them."""
    return app_state(request).capabilities


def tool_categories(request: Source) -> Mapping[str, AbstractToolset[RunDeps]]:
    """The assembled tool-category mapping (core plus every manifest's `toolsets`)."""
    return app_state(request).tool_categories


def gated_tools(request: Request) -> frozenset[str]:
    """The manifests' conditionally-gated tools, which raise `ApprovalRequired` at call time."""
    return app_state(request).gated_tools


def category_availability(request: Source) -> tuple[CategoryAvailability, ...]:
    """The manifests' availability checks, resolved against their categories; an axis of
    the disabled set, asked of the live services every turn."""
    return app_state(request).category_availability


def dormant_categories(request: Request) -> tuple[DormantCategory, ...]:
    """The manifests' dormant-category declarations (category and one-line summary)."""
    return app_state(request).dormant_categories


def dormant_category_names(request: Request) -> frozenset[str]:
    """Just the names, for the catalog surface that only asks which rows are dormant."""
    return frozenset(entry.category for entry in dormant_categories(request))


def dormant_summaries(request: Source) -> dict[str, str]:
    """The declarations as category → summary, the shape a turn is composed from: the
    engine defers by the names and advertises the lines off one mapping, so a group can't
    be withheld unannounced.

    Derived at assembly (`DormantCategory.summaries`): `routes/` sits above `harness/` and
    can't reach the helper.
    """
    return app_state(request).dormant_summaries


def instruction_providers(request: Source) -> tuple[InstructionProvider, ...]:
    """The manifests' dynamic instruction providers, registered on every agent."""
    return app_state(request).instruction_providers


def prompt_context_providers(request: Source) -> tuple[PromptContextProvider, ...]:
    """The manifests' per-turn providers, appended to each user prompt's tail, never persisted."""
    return app_state(request).prompt_context_providers


CONVERSATION_BUSY_DETAIL = "A response is already in progress in this conversation"


#: How long a request that changes a thread waits for the stored parks to be put back.
RESTORE_WAIT_S = 10.0


async def after_restore(request: Request) -> None:
    """Hold a request that changes an existing thread until the stored parks are back: a turn
    finished or a leaf moved first would change where the parked turn is written or occupy
    the thread.

    Raises DegradedCapabilityError (503) after the cap: proceeding would race the restore, and
    a restore stuck on the vault must not hold the request open."""
    try:
        _ = await asyncio.wait_for(registry(request).parks_restored.wait(), RESTORE_WAIT_S)
    except TimeoutError as exc:
        logger.warning(
            "routes: stored parks not restored after %.0fs; refusing %s",
            RESTORE_WAIT_S,
            request.url.path,
        )
        raise DegradedCapabilityError(
            "Paused runs are still being restored after the restart. Try again shortly."
        ) from exc


def claim_conversation(request: Request, conversation_id: str) -> None:
    """Claim ``conversation_id`` for a request that will reposition the active leaf or submit
    a run, with its own ``await``s ahead. Call before the first of them so a second request
    can't slip past a check the first passed but hasn't acted on.

    Raises HTTP 409 when a live run or another claim holds the conversation. Pair it with
    `release_conversation` in a ``finally`` covering every exit (see ``RunRegistry.claim``).
    """
    try:
        registry(request).claim(conversation_id, OPERATOR_ID)
    except ConversationBusyError as exc:
        raise HTTPException(status_code=409, detail=CONVERSATION_BUSY_DETAIL) from exc


def release_conversation(request: Request, conversation_id: str) -> None:
    """Release a claim taken by `claim_conversation`. Idempotent."""
    registry(request).release_and_kick(conversation_id, OPERATOR_ID, inbox_dispatcher(request))


def store(request: Source) -> ConversationStore:
    return app_state(request).conversations


def conversation_search(request: Request) -> ConversationSearch:
    return app_state(request).conversation_search


def corpus(request: Request) -> CorpusIndex:
    return app_state(request).corpus


def models(request: Source) -> ModelRegistry:
    return app_state(request).models


def memory(request: Request) -> MemoryStore:
    return app_state(request).memory


def notifications(request: Request) -> NotificationService:
    return app_state(request).notifications


def conversation_tasks(request: Request) -> ConversationTasks:
    return app_state(request).conversation_tasks


def plan_mode(request: Request) -> PlanMode:
    return app_state(request).plan_mode


def approval_grants(request: Request) -> ApprovalGrantStore:
    return app_state(request).approval_grants


def approval_ledger(request: Request) -> ApprovalLedger:
    return app_state(request).approval_ledger


def egress(request: Request) -> EgressPolicy:
    return app_state(request).egress


def skills(request: Request) -> SkillStore:
    return app_state(request).skills


def platform_skills(request: Request) -> PlatformSkills:
    return app_state(request).platform_skills


def agents(request: Source) -> AgentCatalog:
    return app_state(request).agents


def agent_store(request: Request) -> AgentStore:
    return app_state(request).agent_store


def plugins(request: Request) -> PluginStore:
    return app_state(request).plugins


def commands(request: Source) -> CommandRegistry:
    return app_state(request).commands


def command_store(request: Request) -> CommandStore:
    return app_state(request).command_store


async def composer_tree(
    request: Source, project_id: str | None, conversation_id: str | None
) -> tuple[Path | None, bool]:
    """The checkout a composer in this thread is looking at, and whether it is the thread's
    own worktree rather than the operator's checkout.

    Shared by the `@` picker, the `/` picker and the turn resolving a picked command, so all
    three agree on which tree a name came from; callers indifferent to that use
    :func:`composer_root`.

    Rooted on the project, upgraded by the conversation, and never creating a worktree:
    typing must not acquire the project's single checkout. An unfiled thread or deleted
    project is ``None``, not an error.
    """
    if not project_id:
        return None, False
    try:
        project = await projects(request).get(OPERATOR_ID, project_id)
    except NotFoundError:
        return None, False
    root = Path(project.root_path)
    if not conversation_id:
        return root, False
    return await worktree_root(worktrees(request).path_for(project_id), conversation_id, root)


async def composer_root(
    request: Source, project_id: str | None, conversation_id: str | None
) -> Path | None:
    """:func:`composer_tree` for the callers that only need the tree, not its provenance."""
    root, _from_worktree = await composer_tree(request, project_id, conversation_id)
    return root


def uploads(request: Source) -> UploadStore:
    return app_state(request).uploads


def upload_rate_limiter(request: Request) -> RateLimiter:
    return app_state(request).upload_rate_limiter


def embedding_reindexer(request: Request) -> EmbeddingReindexer:
    return app_state(request).embedding_reindexer


def search(request: Request) -> SearchService:
    return app_state(request).search


def fetcher(request: Request) -> BrowserFetcher:
    return app_state(request).fetcher


def browser(request: Request) -> ManagedBrowser:
    return app_state(request).browser


def searxng(request: Request) -> ManagedSearxng:
    return app_state(request).searxng


def offline(request: Source) -> OfflineModeService:
    return app_state(request).offline


async def disabled_tools(
    request: Source,
    *,
    vision: bool = True,
    kind: str = "chat",
) -> frozenset[str]:
    """Everything withheld from the agent on this run: the operator's disabled set, offline
    mode's web suspension, tools whose results this run's model can't read, tools needing an
    operator a run of this ``kind`` lacks, and tools whose feature was never set up. Every
    route filling ``RunDeps.disabled_tools`` resolves it here so no source is dropped;
    the task executor calls the service directly (no ``Request``).

    The permission level and workspace kind are not part of it: they ride on ``RunDeps`` and
    apply live at the enabled gate because a turn can move them (``services/tool_policy.py``)."""
    return await effective_disabled_tools(
        settings_store(request),
        offline(request),
        OPERATOR_ID,
        vision=vision,
        kind=kind,
        availability=category_availability(request),
        caps=capabilities(request),
    )


def artifacts(request: Request) -> ArtifactStore:
    return app_state(request).artifacts


def workspace_history(request: Request) -> WorkspaceHistoryStore:
    return app_state(request).workspace_history


def machines(request: Request) -> ConversationMachines | None:
    """The per-conversation machines, or None when commands are disabled."""
    return app_state(request).machines


def previews(request_or_ws: Request | WebSocket) -> Previews | None:
    """The live previews, or None when commands are disabled.

    Takes a ``WebSocket`` too: the preview proxy calls it from request and socket handlers,
    which share no base class carrying ``app``.
    """
    return app_state(request_or_ws).previews


def browser_sessions(request: Request) -> BrowserSessionManager | None:
    """The per-conversation browser manager, or None when browser control isn't wired."""
    return app_state(request).browser_sessions


def projects(request: Source) -> ProjectStore:
    return app_state(request).projects


def worktrees(request: Source) -> WorktreeManager:
    return app_state(request).worktrees


#: What ``X-Heph-Project`` must be to mean "everything, unscoped"; an absent header means
#: "use whatever is active", a different request.
PROJECT_SCOPE_ALL = "all"


async def project_scope(request: Request) -> tuple[str | None, ...] | None:
    """The project ids this request may see, or ``None`` for no filter.

    From the ``X-Heph-Project`` header, else the stored active project. Returns the visible
    set, not the active id, so every caller applies one scope rule (``services.projects``).

    A tool never reads this: a run stays in the project it started in even if the operator
    switches away mid-turn, so its scope comes from the conversation (``RunDeps.project_id``).
    """
    header = request.headers.get("X-Heph-Project")
    if header == PROJECT_SCOPE_ALL:
        return None
    active = header or await projects(request).active_id(OPERATOR_ID)
    return visible_project_ids(active)


async def active_project(request: Request) -> str | None:
    """The single project a newly created thing files under (one id, never a set). ``all``
    files nothing: seeing everything says nothing about where new work belongs."""
    header = request.headers.get("X-Heph-Project")
    if header == PROJECT_SCOPE_ALL:
        return None
    return header or await projects(request).active_id(OPERATOR_ID)


def settings_store(request: Source) -> SettingsStore:
    return app_state(request).settings_store


def credentials(request: Request) -> CredentialStore:
    return app_state(request).credentials


def vault(request: Request) -> Vault:
    return app_state(request).vault


def auth_manager(request: Request) -> AuthManager:
    return app_state(request).auth_manager


def require_operator(request: Request) -> None:
    """Refuse a request that did not come from the operator's own session.

    An API token never answers a guarded call, whatever scopes it holds. With
    authentication switched off every caller is the operator.
    """
    if not app_state(request).settings.auth_enabled:
        return
    presented = token_from_headers(
        request.headers.get("authorization"), request.cookies.get(SESSION_COOKIE)
    )
    if not auth_manager(request).verify(presented):
        raise PermissionDeniedError(
            "only the operator can answer this call; an API token cannot approve it"
        )


def workspace_db_intact(request: Request) -> bool:
    """Whether the database survived to this boot; False means the keyfile outlived it.

    Sampled once at startup before the schema is created (`app.py`); `/setup` resets it to
    true, since the workspace it creates is in this database.
    """
    return app_state(request).workspace_db_intact


def mark_workspace_db_intact(request: Request) -> None:
    app_state(request).workspace_db_intact = True


def db_engine(request: Request) -> Engine:
    """The raw DB engine, for surfaces with no dedicated service that use their own rows."""
    return app_state(request).db_engine


def scheduler(request: Request) -> SchedulerService:
    return app_state(request).scheduler


def routines(request: Request) -> RoutineStore:
    return app_state(request).routines


def schedule_parser(request: Request) -> ScheduleNaturalLanguage:
    return app_state(request).schedule_parser


def mail(request: Request) -> MailService:
    """The mail capability: accounts, sync loop, inbox cache, triage and drafts. Its
    background worker starts and stops with the app."""
    return app_state(request).mail


def calendar(request: Request) -> CalendarService:
    """The calendar capability, including natural-language entry as `.nl`."""
    return app_state(request).calendar


def external(request: Request) -> ExternalTools:
    """The external-tools capability: registered MCP servers, configured connectors and
    their shared per-tool trust policy.

    One object because the agent sees one `external` category, and one instance for REST
    and agent alike so a server registered a moment ago is visible to the next run.
    """
    return app_state(request).external


def mcp(request: Request) -> McpRegistry:
    """The registry of external MCP tool servers, backing the `/mcp` surface."""
    return external(request).mcp


def integrations(request: Request) -> IntegrationService:
    """Third-party connectors configured from presets, backing `/integrations`."""
    return external(request).integrations


def secret_vault(request: Request) -> SecretVaultService:
    """The operator's secrets manager, distinct from `vault()` (at-rest key custody). One
    instance per process, since it holds a single in-memory unlock state."""
    return app_state(request).secret_vault


def backup(request: Request) -> BackupService:
    """Encrypted export/import."""
    return app_state(request).backup


def api_tokens(request: Request) -> ApiTokenStore:
    """Inbound scoped API tokens, distinct from `credentials()` (outbound service keys).

    The auth gate in `core.auth` reads this same instance off ``app.state`` (it runs before
    any route), so a revoke here invalidates exactly what the gate trusts."""
    tokens = app_state(request).api_tokens
    if tokens is None:
        raise RuntimeError("the API token store is not wired yet")
    return tokens


def run_terminal_tasks(request: Request) -> set[asyncio.Task[None]]:
    """The shared bucket of in-flight terminal-transition tasks; anything spawning one adds
    it here so shutdown drains it before the DB engine and vault go."""
    return app_state(request).run_terminal_tasks


def settings(request: Request) -> Settings:
    """The process's resolved configuration."""
    return app_state(request).settings


def api_scope_table(request: Request) -> ScopeTable:
    """The scope catalog the `/tokens` surface lists and the auth gate enforces."""
    return app_state(request).api_scope_table


def preview_client(request: Request) -> httpx.AsyncClient:
    """The pooled outbound client the preview proxy forwards through."""
    return app_state(request).preview_client
