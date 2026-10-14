"""Everything a chat turn needs settled before the model is called.

One turn's setup is a sequence of awaits with an order that is load-bearing at almost
every step: the history is read before a fold can shorten it (that is what says whether
the thread is fresh enough to name), the namer starts before the staging so its model call
overlaps the awaits that follow, attachments and the per-turn context resolve before the
fold because they are part of what this turn will cost and the trigger is *projected*, and
the fold lands before the dangling-call strip and the persistence index because that index
must count the list actually handed to the model. Reading that sequence next to the
orchestrator's error handling, its verifier branch and its title epilogue is what made
``engine.py`` hard to change; the sequence is one reason to change, and it is this file.

It fills a :class:`TurnSetup` **in place** rather than returning a new one, because the
orchestrator arms its stop-flush hooks *before* any of this runs — a bound tripping
mid-prelude has to record the operator's bare prompt — and those hooks read
``turn_start``, ``persisted`` and ``stamp_ids`` off that same object at flush time. The
defaults on the dataclass are the correct record for a turn that stopped before it began.

Nothing here reads settings for itself: the orchestrator resolves one object and passes
it, so the fold's threshold, the namer's switch and the gauge all measure against the same
values the rest of the turn does.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from pydantic_ai import ModelMessage, ModelRequest, UserPromptPart
from pydantic_ai.messages import UserContent
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings

from core.config import Settings
from core.container import ServiceContainer
from runs import Run
from services.commands.spec import Invocation
from services.conversation_view import estimate_tokens
from services.conversations import ConversationBinding, ConversationStore
from services.projects import ProjectStore, WorktreeManager
from services.projects.checkout import CheckoutHolders
from services.sandbox import SandboxSessionManager
from services.uploads import UploadStore
from services.workspace import resolve_workspace
from tools import PromptContextProvider, PromptContextRequest, default_workspace_key

from .attachments import resolve_attachments
from .compaction_context import CompactionContext, build_compaction_context
from .factory import TurnAgent
from .file_refs import resolve_file_refs
from .folding import incoming_request, maybe_compact
from .history import (
    TurnStart,
    drop_dangling_tool_calls,
    merge_consecutive_requests,
    with_tail_context,
)
from .injections import announce_injection, contributor_id
from .metrics import post_fold_metrics
from .naming import Namer, TitleContext, start_title
from .summarize import AutoCompactPolicy, build_auto_compact_policy
from .turn import turn_deps


@dataclass
class TurnSetup:
    """What the prelude settles, and what the stop-flush hooks read while it is settling.

    Mutable and filled in place: the orchestrator hands the same object to the hooks it
    arms before the prelude runs, so a bound tripping mid-setup reads whatever has landed
    so far rather than a value captured at arm time.
    """

    # read by the flush hooks from the moment they are armed — defaults are the
    # correct record for a turn that stopped before it began
    turn_start: TurnStart = field(default_factory=TurnStart)
    persisted: list[UserContent] | None = None
    stamp_ids: list[str] = field(default_factory=list)
    #: The `@` references that resolved, stamped on the turn's user request so the
    #: operator's chips come back on a reload. The marker naming them persists with the
    #: prompt; this is the structured form of the same fact, for rendering.
    file_refs: list[str] = field(default_factory=list)
    # filled by prepare_turn
    user_prompt: str | list[UserContent] | None = None
    history: list[ModelMessage] | None = None  # persistence baseline
    model_history: list[ModelMessage] | None = None  # what the model replays
    compaction: CompactionContext | None = None
    policy: AutoCompactPolicy | None = None
    # the namer, started by prepare_turn; None until then, so the orchestrator's
    # `finally` can always call `reap_title(setup.title_namer)`
    title_ctx: TitleContext | None = None
    title_namer: Namer | None = None


def record_prompt(
    setup: TurnSetup,
    run: Run,
    *,
    prompt: str | None,
    command: Invocation | None,
    store: ConversationStore | None,
    conversation_id: str | None,
) -> None:
    """Write the operator's prompt down, once, as the turn begins.

    A turn used to reach the store only when it finished, which left a thread opened
    mid-run with no prompt for the run in flight and let a stop that captured nothing take
    the operator's own words with it. The prompt exists before the model is called, so it is
    recorded here — with the attachments, `@` references and command the turn was sent with,
    stamped exactly as they were when the whole turn was written at the end — and every
    later write (:func:`~agent.finalize.finalize`, the stop-flush hooks, a resume) records
    only what followed it. Its id goes on the turn's boundary, where all of them read it,
    and on the run, where the conversation detail reads it.

    Idempotent, because two callers reach it: the orchestrator once the prelude has settled
    what the prompt carries, and a stop-flush that fires *before* that — which records the
    prompt as far as it got (``setup`` still holds its defaults) rather than losing it.
    ``prompt`` is ``None`` on a regenerate, which answers a request already in the tree:
    nothing is written, and the run is pointed at that request instead by the caller."""
    if (
        prompt is None
        or setup.turn_start.prompt_message_id is not None
        or store is None
        or conversation_id is None
    ):
        return
    content = setup.user_prompt if setup.user_prompt is not None else prompt
    message_id = store.record_prompt(
        conversation_id,
        ModelRequest(parts=[UserPromptPart(content)]),
        attachment_ids=list(setup.stamp_ids),
        file_refs=list(setup.file_refs),
        command=command,
        persisted=setup.persisted,
    )
    setup.turn_start.prompt_message_id = message_id
    run.prompt_message_id = message_id


async def prepare_turn(
    setup: TurnSetup,
    run: Run,
    agent: TurnAgent,
    *,
    prompt: str | None,
    store: ConversationStore | None,
    conversation_id: str | None,
    settings: Settings,
    caps: ServiceContainer,
    uploads: UploadStore | None,
    attachment_ids: list[str] | None,
    file_refs: list[str] | None,
    turn_context: str,
    vision: bool,
    binding: ConversationBinding,
    prompt_context_providers: Sequence[PromptContextProvider],
    auto_compact: AutoCompactPolicy | None,
    disabled_tools: frozenset[str],
    context_window: int | None,
    title_model: Model | None,
    title_settings: ModelSettings | None,
    workspace_key: str = "",
) -> None:
    """Settle everything one chat turn needs before the agent runs, onto ``setup``.

    Loads the thread's history and its readout seeds, resolves what this turn may fold
    with, starts the concurrent namer, stages the operator's attachments, resolves the
    per-turn prompt context, folds the older turns away when the projected footprint calls
    for it, and fixes the persistence boundary against the list the model will actually be
    handed.

    ``turn_context`` is the caller's block for this one invocation (a picked slash
    command's expansion). It joins the per-turn context at the tail, first, and leaves
    with it — none of that is persisted, so the turn on record stays what was typed. On a
    regenerate the caller resolves it again from the invocation stamped on the turn being
    re-answered, and it rides the replayed history's trailing request like the standing
    providers do.
    """

    history = (
        await store.model_history(conversation_id)
        if store is not None and conversation_id is not None
        else None
    )
    # What the thread's earlier turns cost in wall-clock. Read once, here, because it
    # is the one part of the readout that isn't recoverable from the replayed history
    # — every count and token beside it is derived from the messages themselves. A
    # stateless turn has no thread to have spent anything, and keeps the zero default.
    if store is not None and conversation_id is not None:
        run.prior_timings = await store.timings(conversation_id)
        # What this thread's requests weighed besides the conversation, last time one
        # was assembled. Seeded onto the run so the trigger below and every frame
        # emitted before this turn's own measurement lands read the same overhead —
        # a gauge and a fold that disagreed about it would disagree about fullness.
        # `MeasureOverhead` replaces it with the live figure on the first request.
        run.context_overhead = await store.get_overhead(conversation_id)
    # What this turn may fold with. None ⇒ it cannot fold: a stateless run. The policy is
    # resolved either way, because the verifier's size guard measures against the same
    # threshold on every turn. The summary is written on this turn's own agent, with the
    # deps the turn will run it with — the same workspace, level, disabled set and capability
    # bag — so the request renders the brief and offers the tools the turn's first request
    # will, and the engine's cached prefix is the one it reads.
    policy = auto_compact or build_auto_compact_policy(settings)
    compaction = build_compaction_context(
        store=store,
        conversation_id=conversation_id,
        policy=policy,
        agent=agent,
        deps=turn_deps(
            run,
            caps=caps,
            disabled_tools=disabled_tools,
            conversation_id=conversation_id,
            binding=binding,
            workspace_key=workspace_key,
        ),
        settings=settings,
    )
    setup.policy = policy
    setup.compaction = compaction
    # A fresh thread is the one that gets named, and that is settled by whether it had
    # any history at all — read before a fold could shorten it.
    is_first_turn = not history

    # Auto-title context for this run — None disables it (feature off, or no
    # utility model). Built up-front so the title can be generated *concurrently*
    # with the answer (it needs only the operator's opening message), leaving no
    # post-answer "writing" tail. Only a fresh thread's first turn is named.
    setup.title_ctx = (
        TitleContext(title_model, title_settings or {})
        if title_model is not None and settings.title_enabled
        else None
    )
    setup.title_namer = start_title(
        setup.title_ctx if is_first_turn else None,
        prompt,
        run=run,
        store=store,
        conversation_id=conversation_id,
    )

    # Stage any attached files into this conversation's sandbox and append their
    # marker (name, id, mime, size, path) after the operator's prompt — the model
    # reads what it needs from the path rather than receiving the file's text. A
    # vision model additionally gets an image's pixels, the one kind that stays
    # inline in both the live and the persisted shape. Only on a fresh turn: a
    # regenerate (prompt is None) re-runs history, which already carries the markers.
    user_prompt: str | list[UserContent] | None = prompt
    refs_marker = ""

    # Resolved the one way the file tools resolve it, so an attachment lands in — and a
    # reference points at — the very workspace the agent is about to work in: the
    # conversation's sandbox, or its project folder or worktree. Resolved **once**
    # and shared, since both halves below need the same answer and a turn that resolved
    # it twice could resolve it differently under a concurrent acquire.
    workspace = None
    if prompt is not None and (attachment_ids or file_refs):
        workspace = await resolve_workspace(
            workspace_kind=binding.workspace_kind,
            project_id=binding.project_id,
            conversation_id=conversation_id,
            workspace_key=workspace_key or default_workspace_key(conversation_id, run),
            owner_id=run.owner_id,
            sessions=caps.get_optional(SandboxSessionManager),
            projects=caps.get_optional(ProjectStore),
            worktrees=caps.get_optional(WorktreeManager),
            checkouts=caps.get_optional(CheckoutHolders),
            holder=run,
        )

    if attachment_ids and prompt is not None and uploads is not None:
        resolved = await resolve_attachments(
            uploads,
            run.owner_id,
            attachment_ids,
            vision=vision,
            workspace=workspace,
        )
        # Only build a multimodal prompt when something actually resolved — else leave
        # the plain string, so an all-deleted-ids turn doesn't persist as a bare list
        # (which the projection would read as empty text). Stamp only resolved ids as
        # chips; foreign/deleted ids are dropped.
        if resolved.content:
            user_prompt = [prompt, *resolved.content]
        setup.persisted = resolved.persisted or None
        setup.stamp_ids = resolved.ids

    # Files the operator named with `@`. A **reference**, not an injection: the block
    # names the paths and the model reads what it wants with `files_read_file`, which is
    # classified a read and so costs no approval at any level.
    #
    # It rides the prompt and **persists**, exactly like an attachment marker and for the
    # same reason. A marker is a statement of fact — these paths were referenced — so
    # replaying it is honest, where replaying a file's *contents* would be a copy going
    # stale behind the file. Persisting is also what makes a regenerate work without a
    # second mechanism: the marker is already in the history a regenerate replays.
    if file_refs and prompt is not None:
        setup.file_refs, refs_marker = resolve_file_refs(file_refs, workspace)
        if refs_marker:
            base: list[UserContent] = (
                user_prompt
                if isinstance(user_prompt, list)
                else []
                if user_prompt is None
                else [user_prompt]
            )
            user_prompt = [*base, refs_marker]
            # Beside whatever the attachments left, never instead of it: a turn can carry
            # both, and an empty list here is still the "strip to the typed prompt" signal
            # the tail context relies on.
            setup.persisted = [*(setup.persisted or []), refs_marker]

    # Per-turn prompt context (each manifest's `prompt_context` export — the
    # document state): appended at the *tail* of the current turn's user prompt,
    # never persisted, so it's re-resolved fresh each turn with exactly one copy
    # in context — and, unlike an instruction, its churn never touches the head
    # of the request, keeping the whole history a byte-stable cacheable prefix.
    #
    # Announced here rather than from the capability the head's blocks are read
    # from: these resolve before the agent starts, so there is no request to read
    # them back off. One event type either way — the operator's question is what
    # they were not shown, not which seam delivered it.
    context_request = PromptContextRequest(
        caps=caps,
        owner_id=run.owner_id,
        conversation_id=conversation_id,
        workspace_kind=binding.workspace_kind,
        project_id=binding.project_id,
        # The same derivation the file tools use, for the reason `default_workspace_key`
        # exists: a delegated run must be described its own fork, not its parent's files.
        workspace_key=workspace_key or default_workspace_key(conversation_id, run),
    )
    context_texts: list[str] = []
    # The caller's own block for *this* invocation goes first, ahead of the standing
    # per-turn providers: it is what the operator just asked for, and the providers are
    # background the turn happens to carry. Announced under a fixed slug rather than a
    # provider's name, since there is no provider — the chassis put it here on the
    # operator's behalf, which is precisely what the injection row exists to say.
    #
    # Unguarded by `prompt is not None`, unlike the attachment and reference work above,
    # and for the reason the standing providers below are: a **regenerate** has no fresh
    # prompt but still has a turn to compose. Its block rides the trailing request of the
    # replayed history instead (`with_tail_context`, at the foot of this function), which
    # is the same place and the same non-persisted lifetime. Without this a re-answer would
    # be answering a plain `/reviewer …` token — a different question than the first time.
    if turn_context:
        context_texts.append(turn_context)
        announce_injection(run, "command", turn_context, "prompt")
    if refs_marker:
        # Announced, but not appended: it is already on the prompt above. The operator's
        # question — what was put in front of the model that they did not write — is the
        # same either way, and the row is how they see a reference was expanded into a
        # block of instructions about reading files.
        announce_injection(run, "file_refs", refs_marker, "prompt")
    for provider in prompt_context_providers:
        text = await provider(context_request)
        if not text:
            continue
        context_texts.append(text)
        announce_injection(run, contributor_id(provider), text, "prompt")
    if context_texts:
        if prompt is not None:
            context_base: list[UserContent] = (
                user_prompt
                if isinstance(user_prompt, list)
                else []
                if user_prompt is None
                else [user_prompt]
            )
            user_prompt = [*context_base, *context_texts]
            # An empty (non-None) persisted set still strips the live payload back
            # to the typed prompt on record — the tail context must not persist.
            setup.persisted = setup.persisted if setup.persisted is not None else []
    setup.user_prompt = user_prompt

    # Fold the older turns away — *after* the attachments and the per-turn context are
    # resolved, because they are part of what this turn will cost and the trigger is
    # projected: previous footprint + everything about to be added. Measuring the
    # history alone is what forced the old threshold up to 95%, since the incoming turn
    # had to fit in whatever the last one happened to leave spare.
    #
    # And *before* anything downstream measures the list: the rebuild has to land ahead
    # of both the dangling-call strip and `turn_start`, because that index is where
    # `finalize` slices the turn out of `result.all_messages()` — it must count the
    # list actually handed to the model, not the one this started from.
    incoming = incoming_request(user_prompt, context_texts if prompt is None else [])
    folded = False
    if history:
        history, folded = await maybe_compact(
            run,
            compaction,
            history,
            overhead=run.context_overhead,
            incoming_tokens=estimate_tokens([incoming]) if incoming is not None else 0,
            context_window=context_window,
        )
    # A prior turn stopped at a bound persists its transcript verbatim — which can end on
    # an assistant tool call that never got its result. That full record is right for the
    # operator's view, but replaying a dangling tool call to the model is a provider error
    # (an assistant tool_call with no following tool result → HTTP 400), so strip it from
    # the *model's* input here. The persisted transcript is untouched; only this turn's
    # model history is sanitized, and `turn_start` tracks the trimmed length.
    if history:
        history = drop_dangling_tool_calls(history)
        history = merge_consecutive_requests(history)
    setup.history = history
    # Mutated in place, never replaced: the flush hooks armed before this ran hold this
    # very object, and so does everything the turn hands it to.
    setup.turn_start.index = len(history) if history else 0
    if folded:
        # Every response left in the replay reported its prompt size against the
        # history that was just folded away, so none of them describes the thread as it
        # now stands. Marking the whole replay pre-fold makes the frame below read the
        # estimate instead — which is the point of emitting one at all: the operator
        # asked for a fold and must see the gauge fall, not sit at its old figure until
        # the answer lands.
        _ = run.emit(post_fold_metrics(run, history or [], settings=settings))
    # What the model replays — `history` plus, on a regenerate, the per-turn prompt
    # context. `history` itself stays the persistence baseline.
    setup.model_history = history
    if context_texts and prompt is None and history:
        # A regenerate has no fresh prompt — the context rides on the trailing
        # user request in the *model's* view only (`history` itself stays
        # pristine for the verifier's `last_user_text`, and everything before
        # `turn_start` is never re-persisted).
        setup.model_history = with_tail_context(history, context_texts)
