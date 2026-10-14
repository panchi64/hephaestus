"""What the chassis put in front of the model, said out loud.

Every turn carries text nobody in the conversation wrote — a project's instruction files,
the skill catalog, the plan reminder, the date. The gauge has always been able to say what
those cost; these tests pin the other half, which is what they *said* and when they
arrived, because that is the half that accounts for a model behaving as though it had been
told something the transcript never mentions.

Three properties carry the feature and each has its own section below: an injection is
announced once per turn per distinct text (a five-step turn must not echo its own preamble
five times), the token figure is over the whole block even when the wire caps the text, and
the contributor slug is the same one the context gauge files that block under — so the two
surfaces can never name one block two ways.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic_ai import InstructionPart, ModelMessage, ModelRequest, ModelResponse, RunContext
from pydantic_ai.messages import (
    AgentInstructionSource,
    InstructionId,
    TextPart,
    UserContent,
    UserPromptPart,
)
from pydantic_ai.usage import RequestUsage

from agent.injections import (
    AnnounceInjections,
    announce_injection,
    contributor_id,
    injected_tokens,
)
from runs import INJECTED_TEXT_LIMIT, ContextInjected, Run, RunStream
from services.conversations import last_request_usage
from tools import PromptContextRequest, RunDeps

from ._helpers import bodies_of


def _run() -> Run:
    return Run(id="r", kind="chat", owner_id="operator", stream=RunStream())


def _injections(run: Run) -> list[ContextInjected]:
    return bodies_of(run, ContextInjected)


def _announce(announcer: AnnounceInjections, run: Run, parts: list[InstructionPart] | None) -> None:
    """Announce one request's brief onto ``run``, doing what the translator does.

    The capability now hands its bodies to the library's event stream and
    ``agent/translate.py`` puts them on the run; landing them here keeps these tests
    about the two things the capability decides — which parts are worth announcing, and
    which it has already announced this turn — rather than about the envelope.
    """
    for body in announcer.announce(parts):
        _ = run.emit(body)


def _part(name: str | None, content: str) -> InstructionPart:
    if name is None:
        return InstructionPart(content=content)
    return InstructionPart(
        content=content, name=name, id=InstructionId(AgentInstructionSource(), name=name)
    )


# ── The slug ─────────────────────────────────────────────────────────────────────


def test_a_provider_is_filed_under_its_own_name_minus_the_convention():
    """One derivation for the instruction name, the gauge's block id and the injection's
    contributor — which is what stops the popover and the work log from naming the same
    block two different things."""

    def skill_catalog_instructions() -> str: ...
    def repo_instructions() -> str: ...
    def plan_context() -> str: ...

    assert contributor_id(skill_catalog_instructions) == "skill_catalog"
    assert contributor_id(repo_instructions) == "repo"
    assert contributor_id(plan_context) == "plan"


def test_a_provider_with_nothing_left_after_the_strip_is_still_a_row():
    """A name that is all convention and no word strips to the empty string, which is not
    a slug — a row with no id is a row the client can neither label nor key."""

    class Underscore:
        __name__: str = "_"

        def __call__(self) -> str:
            return ""

    assert contributor_id(Underscore()) == "base"


# ── Announced once, and again only when it changed ───────────────────────────────


async def test_the_same_block_is_announced_once_across_a_turns_requests():
    """The hook fires on every model request, so a five-step turn resolves the same brief
    five times. Repeating it would bury the turn's actual work under a fivefold echo of
    its own preamble."""
    run = _run()
    announcer = AnnounceInjections()
    parts = [_part("skill_catalog", "Skills: a, b, c")]
    for _ in range(5):
        _announce(announcer, run, parts)
    assert [i.contributor for i in _injections(run)] == ["skill_catalog"]


async def test_the_dedup_key_keeps_no_copy_of_the_block_it_deduplicates():
    """The capability lives as long as the agent does, and a turn parked for approval
    holds it for as long as the operator takes to answer. A key built from the block
    itself would pin one full copy of every distinct brief for that whole time — a repo
    brief alone is budgeted at 64KB, and the hook fires on every request of the turn."""
    run = _run()
    announcer = AnnounceInjections()
    brief = "".join(f"rule {n}\n" for n in range(8_000))
    for _ in range(25):  # one model request each, the agent's own per-turn limit
        _announce(announcer, run, [_part("repo", brief)])

    assert [i.contributor for i in _injections(run)] == ["repo"]  # still deduplicated
    retained = sum(len(name) + len(digest) for name, digest in announcer.seen)
    assert retained < 100 < len(brief)


async def test_a_block_that_changed_mid_turn_is_announced_again():
    """A plan reminder that grew a task between steps genuinely is a new injection, and
    watching it arrive is the point of putting these on the timeline at all."""
    run = _run()
    announcer = AnnounceInjections()
    _announce(announcer, run, [_part("plan", "1. read the file")])
    _announce(announcer, run, [_part("plan", "1. read the file\n2. write the fix")])
    assert [i.text for i in _injections(run)] == [
        "1. read the file",
        "1. read the file\n2. write the fix",
    ]


async def test_our_own_fixed_brief_is_not_announced():
    """An unnamed part is our literal instructions, or a separator the library joined
    parts with: identical on every turn of every thread, already reported by the gauge as
    `base`, and the one injection an operator can neither act on nor switch off."""
    run = _run()
    _announce(AnnounceInjections(), run, [_part(None, "B" * 400), _part("repo", "CLAUDE.md says")])
    assert [i.contributor for i in _injections(run)] == ["repo"]


async def test_a_provider_that_resolved_to_nothing_injected_nothing():
    """Most providers no-op on most threads (`repo` outside a project folder, `workspace` in
    a sandbox). An empty row would be a row about nothing."""
    run = _run()
    _announce(AnnounceInjections(), run, [_part("workspace", "")])
    assert _injections(run) == []


async def test_nothing_to_read_is_not_a_crash():
    run = _run()
    _announce(AnnounceInjections(), run, None)
    assert _injections(run) == []


# ── The figure, and what the wire carries ────────────────────────────────────────


async def test_the_token_figure_covers_the_whole_block_even_when_the_text_is_capped():
    """The cap exists so a 60KB instruction file doesn't ride the replay buffer to every
    reattaching client. It must not make the block look cheaper than it was — the operator
    reads the figure to decide whether to switch the contributor off."""
    run = _run()
    whole = "x " * INJECTED_TEXT_LIMIT
    announce_injection(run, "repo", whole, "instructions")

    (injected,) = _injections(run)
    assert injected.truncated is True
    assert len(injected.text) <= INJECTED_TEXT_LIMIT
    assert injected.tokens == injected_tokens(whole)
    assert injected.tokens > injected_tokens(injected.text)


async def test_a_block_inside_the_cap_is_carried_whole_and_says_so():
    run = _run()
    announce_injection(run, "date", "Today is Sunday, August 30, 2026.", "instructions")

    (injected,) = _injections(run)
    assert injected.text == "Today is Sunday, August 30, 2026."
    assert injected.truncated is False
    assert injected.placement == "instructions"


async def test_where_it_landed_travels_with_it():
    """Head and tail cost differently — churn at the head invalidates the prompt-prefix
    cache from byte 0 — so which one it was is not something the client should guess."""
    run = _run()
    announce_injection(run, "plan", "1. read the file", "prompt")
    assert _injections(run)[0].placement == "prompt"


# ── The whole chain, on a real turn ──────────────────────────────────────────────


async def test_a_live_turn_announces_both_seams_before_it_answers():
    """Instructions resolve inside the library and are read back off the request; per-turn
    prompt context resolves in the engine before the agent starts. Two seams, one event
    type — and both must land ahead of the work they shaped."""
    from pydantic_ai.models.test import TestModel

    from agent import build_chat_orchestrator
    from runs import RunRegistry, RunStatus

    async def skill_catalog_instructions(_ctx: RunContext[RunDeps]) -> str:
        return "Skills available: writing, research."

    async def plan_context(_req: PromptContextRequest) -> str:
        return "Your current plan: 1. answer the question."

    orch = build_chat_orchestrator(
        "hi",
        model=TestModel(custom_output_text="ok", call_tools=[]),
        categories={},
        instruction_providers=[skill_catalog_instructions],
        prompt_context_providers=[plan_context],
        context_window=100_000,
    )
    run = RunRegistry().submit(kind="chat", owner_id="operator", orchestrator=orch)
    await run.wait()
    assert run.status is RunStatus.done

    events = run.stream.replay()
    injected = {e.body.contributor: e.body for e in events if e.body.type == "context.injected"}
    assert injected.keys() >= {"skill_catalog", "plan", "date"}
    assert injected["skill_catalog"].placement == "instructions"
    assert injected["skill_catalog"].text == "Skills available: writing, research."
    assert injected["plan"].placement == "prompt"
    assert injected["plan"].text == "Your current plan: 1. answer the question."

    # Ahead of the work: an injection the operator finds *after* the answer explains
    # nothing about an answer they have already read.
    first_answer = next(e.seq for e in events if e.body.type == "answer.delta")
    assert all(e.seq < first_answer for e in events if e.body.type == "context.injected")


# ── The request beside the split ─────────────────────────────────────────────────


def _response(
    input_tokens: int = 0, output_tokens: int = 0, cache_read_tokens: int = 0
) -> ModelResponse:
    return ModelResponse(
        parts=[TextPart(content="ok")],
        model_name="qwen3-32b",
        provider_name="lmstudio",
        usage=RequestUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
        ),
    )


def test_the_last_request_reports_the_route_that_actually_answered():
    """On a fallback chain the model that answered is not necessarily the one the thread
    is bound to, and a cumulative total cannot tell the two apart."""
    messages = [
        ModelRequest(parts=[UserPromptPart(content="hi")]),
        _response(input_tokens=100),
        ModelRequest(parts=[UserPromptPart(content="again")]),
        _response(input_tokens=400, output_tokens=20, cache_read_tokens=380),
    ]
    last = last_request_usage(messages)
    assert last is not None
    assert last.route == "lmstudio:qwen3-32b"
    assert last.input_tokens == 400
    assert last.output_tokens == 20
    assert last.cache_read_tokens == 380


def test_an_endpoint_that_reports_no_caching_reports_absent_not_zero():
    """A 0 here would read as "your caching is broken" rather than "nobody said", and
    most OpenAI-compatible and local endpoints say nothing at all."""
    last = last_request_usage([_response(input_tokens=100)])
    assert last is not None
    assert last.cache_read_tokens is None
    assert last.cache_write_tokens is None
    assert last.output_tokens is None


def test_a_thread_with_no_response_has_no_request_to_report():
    assert last_request_usage([ModelRequest(parts=[UserPromptPart(content="hi")])]) is None


# ── The tail seam ────────────────────────────────────────────────────────────────
#
# Per-turn context goes at the *end* of the current turn's user prompt and is never
# persisted. That placement is a prompt-cache decision and it is the optimum rather than a
# compromise, but only while two things hold: the tail really is last in the request, and it
# really does not reach the durable history. Break the first and it stops being a suffix, so
# everything after it re-prefills on every turn instead of once per turn boundary. Break the
# second and the transcript asserts the operator wrote something they never wrote, and the
# footprint grows by one stale copy per turn.


def _tail_content(view: list[ModelMessage]) -> str | Sequence[UserContent]:
    last = view[-1]
    assert isinstance(last, ModelRequest)
    part = last.parts[-1]
    assert isinstance(part, UserPromptPart)
    return part.content


def _regenerated(texts: list[str]) -> list[ModelMessage]:
    from agent.history import with_tail_context

    history: list[ModelMessage] = [
        ModelRequest(parts=[UserPromptPart(content="the first ask")]),
        ModelResponse(parts=[TextPart(content="the first answer")], usage=RequestUsage()),
        ModelRequest(parts=[UserPromptPart(content="the ask being regenerated")]),
    ]
    return with_tail_context(history, texts)


def test_the_tail_is_the_last_content_in_the_request():
    """The property Stage 2's whole analysis rests on. Anything appended *after* the tail
    would sit behind a block that changes every turn, so it would be re-read every turn
    rather than shared — which is exactly what putting volatile content in the head does,
    only further down."""
    view = _regenerated(["## Your task list\n- [ ] one"])
    content = _tail_content(view)

    assert isinstance(content, list)
    assert content[0] == "the ask being regenerated"
    last = content[-1]
    assert isinstance(last, str)
    assert last.startswith("## Your task list")


def test_the_tail_never_reaches_the_message_it_was_appended_to():
    """`with_tail_context` rebuilds rather than mutating, because the store's in-memory tree
    shares these objects — so a mutation here writes the chassis's own text into the
    operator's durable transcript, where it reads as something they typed."""
    original = ModelRequest(parts=[UserPromptPart(content="the ask being regenerated")])
    history: list[ModelMessage] = [original]

    from agent.history import with_tail_context

    view = with_tail_context(history, ["per-turn context"])

    assert view[-1] is not original
    assert history[-1] is original
    part = history[-1].parts[0]
    assert isinstance(part, UserPromptPart)
    assert part.content == "the ask being regenerated"


def test_a_regenerate_puts_the_tail_where_a_fresh_turn_does():
    """The two paths have to agree, or a regenerate diverges from the turn it is
    regenerating at a different position and pays for the difference. A fresh turn appends
    the context to the prompt it was handed; a regenerate has no fresh prompt, so it rides
    the trailing request — and the result is the same shape: the operator's words first, the
    chassis's after."""
    texts = ["first block", "second block"]
    view = _regenerated(texts)
    content = _tail_content(view)

    # The same list a fresh turn's `user_prompt` would carry: the typed text, then each
    # provider's block in registration order.
    assert content == ["the ask being regenerated", *texts]


def test_a_history_with_no_trailing_request_is_left_alone():
    """Defensive, and it must stay so: a regenerate whose history ends in a response has
    nowhere to put the tail, and inventing a message for it would put chassis text in the
    conversation under no author at all."""
    from agent.history import with_tail_context

    history: list[ModelMessage] = [
        ModelResponse(parts=[TextPart(content="answer")], usage=RequestUsage())
    ]
    assert with_tail_context(history, ["context"]) == history
