"""Shared accessors for the singletons hung on ``app.state``.

One place to resolve a capability from a request, so every router reaches them
the same way and the wiring has a single point to change (or to grow into
FastAPI ``Depends`` later).
"""

from __future__ import annotations

import asyncio
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
from core.exceptions import NotFoundError, PermissionDeniedError
from core.ratelimit import RateLimiter
from core.vault import Vault
from runs import ConversationBusyError, InboxDispatch, Run, RunRegistry
from services.agents import AgentCatalog, AgentStore
from services.api_token_store import ApiTokenStore
from services.approval_grants import ApprovalGrantStore
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
from services.sandbox import SandboxSessionManager
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
from services.vm import PlatformVM
from services.webfetch import BrowserFetcher, ManagedBrowser
from services.work_summaries import WorkSummaryService
from services.workspace_history import WorkspaceHistoryStore

if TYPE_CHECKING:
    from harness.manifest import DormantCategory, FeatureManifest
    from tools import InstructionProvider, PromptContextProvider, RunDeps

# Single operator: every record is attributed to this owner until a second human
# exists (the ownership seam). One constant so routes don't each redefine it.
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
        self.sandbox: SandboxSessionManager | None
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
        self.vm: PlatformVM | None
        self.subagent_records: SubagentStore | None
        self.conversation_teardown: ConversationTeardown
        self.discovery_client: httpx.AsyncClient
        self.work_summaries: WorkSummaryService
        self.corpus_uploads: UploadsAdapter
        self.corpus_folder: FolderAdapter
        self.corpus_chunk_store: CorpusChunkStore
        self.task_run_waiters: dict[str, asyncio.Future[Run]]


#: What an accessor resolves app state from. The app itself is how a turn the inbox starts
#: (``routes/chat/queued.py``) reaches the same handles a request does.
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


def vm(request: Request) -> PlatformVM | None:
    """The platform VM, or None when this deployment never built one."""
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
    """The app's one agent-facing capability bag (`RunDeps.caps`) — assembled at
    startup from every feature manifest's `capabilities` export. Every run path
    (live chat, approval resume, the scheduler's executor) hands this same bag to
    the engine, so the capability set can never diverge between them."""
    return app_state(request).capabilities


def tool_categories(request: Source) -> Mapping[str, AbstractToolset[RunDeps]]:
    """The assembled tool-category mapping (core + every enabled manifest's
    `toolsets` export) — what the agent runs against and the catalog routes list."""
    return app_state(request).tool_categories


def gated_tools(request: Request) -> frozenset[str]:
    """The union of the manifests' conditionally-gated tool names — the call-time
    `ApprovalRequired` raisers the approval-scope vocabulary must carry."""
    return app_state(request).gated_tools


def category_availability(request: Source) -> tuple[CategoryAvailability, ...]:
    """The manifests' availability checks, resolved against the categories they register
    — the seventh axis of the disabled set, asked of the live services on every turn."""
    return app_state(request).category_availability


def dormant_categories(request: Request) -> tuple[DormantCategory, ...]:
    """The manifests' dormant-category declarations (category + one-line summary) — the
    groups whose schemas the agent loads for itself, and the index it is shown."""
    return app_state(request).dormant_categories


def dormant_category_names(request: Request) -> frozenset[str]:
    """Just the category names, for the catalog surface that only asks which rows are
    dormant."""
    return frozenset(entry.category for entry in dormant_categories(request))


def dormant_summaries(request: Source) -> dict[str, str]:
    """The same declarations as category → summary, which is the shape a turn is composed
    from: the engine reads the names to defer and the lines to advertise off one mapping,
    so a group cannot be withheld without being announced.

    Derived at assembly (`DormantCategory.summaries`) rather than spelled again here —
    `routes/` sits above `harness/` and cannot reach the helper, and the comprehension is
    exactly the one every composer of this seam would otherwise write slightly differently.
    """
    return app_state(request).dormant_summaries


def instruction_providers(request: Source) -> tuple[InstructionProvider, ...]:
    """The manifests' dynamic instruction providers, registered on every agent."""
    return app_state(request).instruction_providers


def prompt_context_providers(request: Source) -> tuple[PromptContextProvider, ...]:
    """The manifests' per-turn prompt-context providers, appended to the tail of every
    turn's user prompt (never persisted) — the cache-friendly home for volatile context."""
    return app_state(request).prompt_context_providers


CONVERSATION_BUSY_DETAIL = "A response is already in progress in this conversation"


def claim_conversation(request: Request, conversation_id: str) -> None:
    """Claim ``conversation_id`` for a request that will reposition the active leaf
    (regenerate/edit/rewind/switch-version/a purging delete) or submit a new run, with
    further ``await``s of its own still ahead of it — call this **before** the first of
    those ``await``s, so a second near-simultaneous request can never slip past a check
    the first request has already passed but not yet acted on.

    Raises HTTP 409 when a live run already drives this conversation, or another
    in-flight request already holds the claim. The caller **must** pair this with
    `release_conversation` in a ``finally`` covering every exit path (a failed model
    resolve, a 404 message id, a successful submit) — see ``RunRegistry.claim``.
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

    The question three surfaces ask — the `@` picker listing files, the `/` picker offering
    what the project declares, and the turn that has to resolve a picked command against the
    same tree the menu listed from. Answering it in three places would mean three chances to
    disagree about which tree a name came from, so it is answered here; the two that do not
    care which tree answered call :func:`composer_root` instead.

    **Rooted on the project, upgraded by the conversation**, and never creating a worktree:
    typing a character must not acquire the project's single checkout. An unfiled thread, or
    a project that has since been deleted, is ``None`` rather than an error — nothing on
    either surface is worth failing a keystroke over.
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
    """Everything withheld from the agent on this run — the operator's own disabled set
    (`AE-3.3`) unioned with offline mode's automatic web suspension, the ones this run's model can't
    read the results of, the ones that need an operator a run of this ``kind`` doesn't have, and the
    ones whose feature the operator has never set up. Every route that fills
    ``RunDeps.disabled_tools`` resolves it here, so a run path can't apply one source and drop the
    others; ``app.py``'s task executor calls the service directly (it has no ``Request``).

    The thread's **permission level and workspace kind are not part of this**: both ride on
    ``RunDeps`` and are applied live at the enabled gate, because a turn can move them
    itself (``services/tool_policy.py`` explains why that keeps them out of the union)."""
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


def sandbox_sessions(request_or_ws: Request | WebSocket) -> SandboxSessionManager | None:
    """The per-conversation sandbox manager, or None when code execution is
    disabled (fail closed).

    Accepts a ``WebSocket`` as well as a ``Request``: the preview proxy reaches this
    from both a request handler and a socket handler, and the two share no base class
    that carries ``app``.
    """
    return app_state(request_or_ws).sandbox


def browser_sessions(request: Request) -> BrowserSessionManager | None:
    """The per-conversation browser manager, or None when browser control isn't wired
    (the feature is off, or its manifest never assembled)."""
    return app_state(request).browser_sessions


def projects(request: Source) -> ProjectStore:
    return app_state(request).projects


def worktrees(request: Source) -> WorktreeManager:
    return app_state(request).worktrees


#: What ``X-Heph-Project`` must be to mean "show me everything, unscoped". A literal
#: rather than an absent header, because absent means "use whatever is active" — the two
#: are different requests and a client must be able to say either.
PROJECT_SCOPE_ALL = "all"


async def project_scope(request: Request) -> tuple[str | None, ...] | None:
    """The project ids this request may see, or ``None`` for no filter at all.

    Resolved from the ``X-Heph-Project`` header when the client sent one, else from the
    operator's stored active project. It returns the *visible set* rather than the active
    id so that every caller applies the one scope rule (``services.projects``) instead of
    each route re-deriving "unfiled plus this one" and one of them eventually getting it
    wrong.

    Note the deliberate asymmetry with ``RunDeps.project_id``: a **tool** never reads
    this. A run keeps working in the project it started in even if the operator switches
    away mid-turn, so the run's scope comes from the conversation, not the live request.
    """
    header = request.headers.get("X-Heph-Project")
    if header == PROJECT_SCOPE_ALL:
        return None
    active = header or await projects(request).active_id(OPERATOR_ID)
    return visible_project_ids(active)


async def active_project(request: Request) -> str | None:
    """The single project a *newly created* thing files itself under.

    The scope above answers "what may I see"; this answers "where does a new row go",
    which is one id, never a set. ``all`` files nothing — asking to see everything is not
    a statement about where new work belongs.
    """
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
    """Whether the database backing this workspace survived to this boot.

    False means the keyfile outlived its database — the operator cleared `app.db`
    expecting a reset and got a password prompt instead. Sampled once at startup
    before the schema is created (see `app.py`); `/setup` sets it back to true, since
    the workspace it just created *is* in this database.
    """
    return app_state(request).workspace_db_intact


def mark_workspace_db_intact(request: Request) -> None:
    app_state(request).workspace_db_intact = True


def db_engine(request: Request) -> Engine:
    """The raw DB engine — for the surfaces (like `routes/tasks.py`) that don't yet
    have a dedicated service and read/write their own SQLModel rows directly."""
    return app_state(request).db_engine


def scheduler(request: Request) -> SchedulerService:
    return app_state(request).scheduler


def routines(request: Request) -> RoutineStore:
    return app_state(request).routines


def schedule_parser(request: Request) -> ScheduleNaturalLanguage:
    return app_state(request).schedule_parser


def mail(request: Request) -> MailService:
    """The mail capability (`EMAIL-1..5`) — accounts, the sync loop, the inbox cache,
    triage and drafts. Its background worker is started and stopped with the app."""
    return app_state(request).mail


def calendar(request: Request) -> CalendarService:
    """The calendar capability (`CAL-1..3`), including natural-language entry as `.nl`."""
    return app_state(request).calendar


def external(request: Request) -> ExternalTools:
    """The whole external-tools capability — registered MCP servers (`MCP-*`), configured
    connectors (`INTEG-*`) and the per-tool trust policy they share (`AE-3.6`).

    One object rather than two capability handles, because the agent sees one `external`
    category and shouldn't have to know which source a tool came from. One instance for
    both the REST surfaces and the agent, which is what makes a server registered a moment
    ago visible to the very next run.
    """
    return app_state(request).external


def mcp(request: Request) -> McpRegistry:
    """The registry of external MCP tool servers (`MCP-*`), backing the `/mcp` surface."""
    return external(request).mcp


def integrations(request: Request) -> IntegrationService:
    """Third-party connectors configured from presets (`INTEG-*`), backing `/integrations`."""
    return external(request).integrations


def secret_vault(request: Request) -> SecretVaultService:
    """The operator's secrets manager (`VAULT-*`). Distinct from `vault()` above, which is
    the at-rest key custody. One instance per process — its whole point is holding a single
    in-memory unlock state, which a per-request instance would throw away."""
    return app_state(request).secret_vault


def backup(request: Request) -> BackupService:
    """Encrypted export/import (`BACKUP-*`)."""
    return app_state(request).backup


def api_tokens(request: Request) -> ApiTokenStore:
    """Inbound scoped API tokens (`AUTH-4`). Distinct from `credentials()` above, which
    holds the outbound service keys.

    The auth gate in `core.auth` reads this same instance straight off ``app.state`` —
    it has to, since it runs before any route reaches this module — so a revoke through
    the route invalidates exactly what the gate trusts, rather than the two keeping
    separate verification caches."""
    tokens = app_state(request).api_tokens
    if tokens is None:
        raise RuntimeError("the API token store is not wired yet")
    return tokens


def run_terminal_tasks(request: Request) -> set[asyncio.Task[None]]:
    """The shared bucket of in-flight terminal-transition background tasks — anything
    that spawns one adds it here so shutdown can drain it alongside the notification
    surface's own, rather than tearing the DB engine/vault down from under it."""
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
