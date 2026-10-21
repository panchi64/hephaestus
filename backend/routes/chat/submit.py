"""Submitting a turn on the operator's behalf: the thread's ``TurnScope``, the picked slash
command's tail block, and the operator's settings resolved once for every interactive
turn (send, regenerate, edit, and one the inbox starts) before handing off to
``compose_turn``. Everything resolves from a request or from the app itself."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from fastapi import HTTPException
from pydantic_ai import AbstractToolset

from agent.engine import recompose_turn
from agent.recompose import ParkedFacts, Recomposed, RecomposeError
from agent.summarize import AutoCompactPolicy, resolve_auto_compact_policy
from core.container import ServiceContainer
from routes import deps
from routes.chat.schemas import ChatCreated
from routes.chat.turn import TurnModels, compose_turn, resolve_turn_models
from routes.deps import OPERATOR_ID
from services.agents import AgentDefinition, lower_of
from services.agents.thread import ThreadAgent, agent_is_gone, resolve_thread_agent
from services.commands import Invocation, expand
from services.conversations import ConversationBinding
from services.settings_store import (
    get_context_thresholds,
    get_inactivity_timeout,
    get_wall_clock_timeout,
)
from services.workspace import place_kind
from tools import InstructionProvider, RunDeps


async def resolve_models(
    request: deps.Source,
    endpoint_id: str | None,
    model: str | None,
    *,
    agent: AgentDefinition | None = None,
) -> TurnModels:
    """The turn's models: the request's own pick, else the agent's, else the ``main``
    role."""
    if endpoint_id is None and model is None and agent is not None:
        endpoint_id, model = agent.endpoint_id, agent.model
    return await resolve_turn_models(deps.models(request), endpoint_id, model)


async def thread_models(
    request: deps.Source, conversation_id: str, endpoint_id: str | None, model: str | None
) -> TurnModels:
    """The models a turn on an existing thread runs on, its agent's pick included."""
    binding = await deps.store(request).binding(conversation_id)
    applied = await thread_agent(request, binding, conversation_id)
    return await resolve_models(request, endpoint_id, model, agent=applied.agent)


async def thread_agent(
    request: deps.Source, binding: ConversationBinding, conversation_id: str | None
) -> ThreadAgent:
    """The agent this thread runs, applied to its binding."""
    return await resolve_thread_agent(
        deps.agents(request),
        OPERATOR_ID,
        binding,
        conversation_id,
        projects=deps.projects(request),
        worktrees=deps.worktrees(request),
    )


async def _command_context(
    request: deps.Source,
    command: Invocation | None,
    conversation_id: str,
    binding: ConversationBinding,
    disabled_tools: frozenset[str],
) -> str:
    """The tail block a picked command contributes to this turn, or ``""``.

    **A name that resolves to nothing is not an error.** The catalog narrows on facts that
    move between the picker's read and the send — a skill unpublished in another tab, a
    tool the operator just switched off — and failing the turn over one would throw away
    the message they typed to punish them for a race they did not cause. The prompt still
    carries their literal `/name`, which is a perfectly legible thing for the model to
    receive and ask about, so the turn goes through with nothing added.

    The same latitude is what makes **regenerate** honest rather than merely convenient.
    A regenerate arrives here with the invocation read back off the turn it is re-answering,
    and resolves it *now*: an edited template takes effect, an unpublished skill quietly
    stops expanding, and neither outcome is a failure — re-answering a turn has always
    meant re-running it against the world as it currently stands.
    """
    if command is None:
        return ""
    spec = await deps.commands(request).resolve(
        OPERATOR_ID,
        command.name,
        place=place_kind(binding.project_id, binding.worktree),
        has_conversation=True,
        disabled_tools=disabled_tools,
        # The same tree the picker listed from, resolved the same way — a project's own
        # `/release-notes` must mean the same thing on the way out as it did in the menu.
        root=await deps.composer_root(request, binding.project_id, conversation_id),
    )
    return expand(spec, command.argument) if spec is not None else ""


@dataclass(frozen=True)
class TurnScope:
    """What decides the agent a turn on this thread runs and the deps it runs it with: the
    thread's binding, the tools withheld from it, and the app's assembled catalog,
    instructions and capability bag.

    Gathered in one place because two things need the identical answer — a chat turn, and
    the operator's own fold, whose summary is written on the agent that turn would run so
    its request matches the one the model last served."""

    binding: ConversationBinding
    agent: AgentDefinition
    disabled_tools: frozenset[str]
    capabilities: ServiceContainer
    categories: Mapping[str, AbstractToolset[RunDeps]]
    dormant: Mapping[str, str]
    instruction_providers: Sequence[InstructionProvider]


async def turn_scope(request: deps.Source, conversation_id: str, *, vision: bool) -> TurnScope:
    """This thread's :class:`TurnScope`, read off the `Request`.

    ``vision`` is the resolved main model's image fact: a tool that answers with an image
    is withheld from a model that can't read one."""
    # Read once: where the run works and how far it may go must never be resolved apart.
    binding = await deps.store(request).binding(conversation_id)
    applied = await thread_agent(request, binding, conversation_id)
    return TurnScope(
        binding=applied.binding,
        agent=applied.agent,
        disabled_tools=await deps.disabled_tools(request, vision=vision) | applied.withheld,
        # The app's one agent-facing capability bag — assembled at startup from every
        # feature manifest's `capabilities` export, so a turn never enumerates handles.
        capabilities=deps.capabilities(request),
        # The assembled tool catalog + the manifests' dynamic instructions — read per
        # request so the turn always runs against what the app assembled.
        categories=deps.tool_categories(request),
        dormant=deps.dormant_summaries(request),
        instruction_providers=(*deps.instruction_providers(request), *applied.instructions),
    )


async def submit_turn(
    request: deps.Source,
    *,
    prompt: str | None,
    conversation_id: str,
    models: TurnModels,
    attachment_ids: list[str] | None = None,
    file_refs: list[str] | None = None,
    command: Invocation | None = None,
    ephemeral: bool = False,
) -> ChatCreated:
    """Gather this route's resources from the `Request` and hand off to `compose_turn`.

    Async only because the enabled-tool policy is a persisted read; every other resource
    here is an `app.state` handle. `compose_turn` itself stays synchronous, so the
    submit remains a single uninterrupted step after the caller's conversation mutation."""
    # `models[4]` is the resolved main model's vision fact — the same one `compose_turn`
    # passes to the engine for attachments.
    #
    # Hoisted out of the call below because the command catalog needs the disabled set too:
    # a `/reviewer` is an offer to launch a sub-agent, and where `agents_spawn` has been
    # withheld from this run there is nothing to offer. Resolving it twice would let the
    # command the operator picked and the tools the turn actually has disagree.
    scope = await turn_scope(request, conversation_id, vision=models[4])
    return compose_turn(
        prompt=prompt,
        conversation_id=conversation_id,
        models=models,
        # Resolved here rather than in the engine, for the same reason the enabled-tool
        # policy is: it is a persisted read, and `compose_turn` is the synchronous step
        # after the caller has already mutated the conversation.
        turn_context=await _command_context(
            request, command, conversation_id, scope.binding, scope.disabled_tools
        ),
        # Written onto the turn as well as spoken into it — see `compose_turn`. Harmless on
        # a regenerate, which records no user request for it to land on.
        command=command,
        capabilities=scope.capabilities,
        registry=deps.registry(request),
        store=deps.store(request),
        uploads=deps.uploads(request),
        categories=scope.categories,
        dormant=scope.dormant,
        instruction_providers=scope.instruction_providers,
        prompt_context_providers=deps.prompt_context_providers(request),
        disabled_tools=scope.disabled_tools,
        binding=scope.binding,
        attachment_ids=attachment_ids,
        file_refs=file_refs,
        ephemeral=ephemeral,
        # Resolved here, not at each caller: send, regenerate and edit all want it and none
        # of them should have to know it exists.
        auto_compact=await _resolve_auto_compact(request, conversation_id),
        # Same again for the context gauge's boundaries: resolved once here so send,
        # regenerate and edit all report severity against the operator's own pair.
        context_thresholds=await get_context_thresholds(deps.settings_store(request), OPERATOR_ID),
        # Same reason again: every interactive turn runs under the operator's inactivity
        # bound (else the config default).
        inactivity_timeout_s=await get_inactivity_timeout(
            deps.settings_store(request), OPERATOR_ID
        ),
        # And the wall clock, which is normally absent — resolved here anyway so that an
        # operator who does set one gets it on every interactive turn, not just send.
        wall_clock_timeout_s=lower_of(
            scope.agent.wall_clock_timeout_s,
            await get_wall_clock_timeout(deps.settings_store(request), OPERATOR_ID),
        ),
    )


async def recompose_chat(source: deps.Source, facts: ParkedFacts) -> Recomposed:
    """The agent and compaction context a parked chat turn resumes with after a restart,
    composed from the same :class:`TurnScope` a fresh turn on the thread would be, on the
    endpoint and model the parked agent held."""
    conversation_id = facts.conversation_id
    if conversation_id is None:
        raise RecomposeError("this run had no conversation to resume in")
    try:
        binding = await deps.store(source).binding(conversation_id)
        applied = await thread_agent(source, binding, conversation_id)
        if agent_is_gone(applied, binding):
            raise RecomposeError(f"the agent {binding.agent_name!r} is gone")
        models = await resolve_models(source, facts.endpoint_id, facts.model, agent=applied.agent)
    except HTTPException as exc:
        raise RecomposeError(str(exc.detail)) from exc
    scope = await turn_scope(source, conversation_id, vision=models.vision)
    return await recompose_turn(
        facts,
        model=models.main,
        categories=scope.categories,
        dormant=scope.dormant,
        instruction_providers=scope.instruction_providers,
        capabilities=scope.capabilities,
        store=deps.store(source),
        disabled_tools=scope.disabled_tools,
        auto_compact=await _resolve_auto_compact(source, conversation_id),
        context_window=models.context_window,
        context_thresholds=await get_context_thresholds(deps.settings_store(source), OPERATOR_ID),
        title_model=models.utility,
        title_settings=models.utility_settings,
        endpoint=models.endpoint,
    )


async def _resolve_auto_compact(
    request: deps.Source, conversation_id: str | None
) -> AutoCompactPolicy:
    """The effective conversation auto-compaction policy for a turn: the thread's on/off
    override beats the operator's global default, which beats the config default —
    resolved here rather than in the engine so the orchestrator never reads the settings
    store itself."""
    override = (
        await deps.store(request).get_compaction_override(conversation_id)
        if conversation_id is not None
        else None
    )
    return await resolve_auto_compact_policy(
        deps.settings_store(request), OPERATOR_ID, override=override
    )
