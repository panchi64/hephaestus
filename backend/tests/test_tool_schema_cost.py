"""What the tool catalog costs before the model has read a single message.

Every tool the model is offered arrives as name + description + JSON schema, at the head
of every request, whether or not the turn goes anywhere near it. That is the one part of
the window nobody chose and nobody sees, so it is measured here — against the *real*
assembled catalog (core plus every manifest's export), through the same
``agent.overhead`` sizing the context gauge draws from, so the unit here and the unit the
operator reads stay one unit.

Since the dormant categories landed there are two numbers, not one. A **fresh request**
carries ~35.6k characters across 34 tools — call it ~8.7k tokens — because five categories
(``browse``, ``calendar``, ``mail``, ``research``, ``vault``) ship with their schemas
withheld until the model asks for the group. The **corpus** behind it, every dormant group
revealed, is ~68k across 70; ``browse`` alone is 18 tools and most of the difference, which
is why it is dormant. A Plan-level turn is handed less of the corpus than an acting one, because
everything above ``read`` is withheld outright rather than offered and refused — but not
the ``plan`` and ``tasks`` categories, which a read-only turn needs precisely because it
is read-only.

Three things are pinned. A **ceiling on the fresh request**, which is what a turn actually
pays. A looser **ceiling on the corpus**, because a tool added to a dormant group is
invisible to the first ceiling and still costs the turn that opens the group. And the
**narrowing**, so the withholding that makes Plan cheap stays real: it is enforcement first
and a saving second, and a regression would be invisible from either side alone. A failure
here is not necessarily a bug, but it is always a decision somebody should make
deliberately.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping

from pydantic_ai import RunContext
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from agent import stream_agent_run
from agent.code_mode import code_mode_capability
from agent.factory import NO_DORMANT, build_agent
from agent.overhead import measure_overhead
from core.db import init_db, make_engine
from core.text import CHARS_PER_TOKEN_JSON
from harness.discovery import discover_manifests
from runs import Run, RunStream
from services.permissions import PERMISSION_LADDER, PermissionLevel
from services.settings_store import SettingsStore
from services.tool_policy import effective_disabled_tools
from services.workspace_kind import WORKSPACE_TOOLS, WorkspaceKind
from tools import RunDeps, build_agent_toolsets, core_categories

from ._helpers import full_tool_categories
from .test_tool_policy import stub_offline

#: What a fresh request's catalog may cost, in characters of serialized schema. Set a
#: little above today's ~35.6k so an ordinary addition lands quietly and a category-sized one
#: does not. Characters rather than tokens for the same reason the measurement itself uses
#: them: no tokenizer, no provider, no drift.
#:
#: **Raised once, from 26k, for the `plan` category** — the three tools that let a thread
#: enter plan mode, submit a plan and read the one it agreed to. They cost every request
#: about 1.5k characters, and they are the one addition that could not be paid for by
#: deferral: a model that has to reveal a dormant group before it can propose planning is a
#: model that has already started doing the work instead. The saving that would have come
#: from making them dormant is real and was declined on purpose.
#:
#: **Raised again, from 28k, for the per-call `narration` argument** — one string property,
#: with its instruction, on every tool that does more than read (``tools/describe.py``). It
#: took a fresh request from 25.9k to 30.1k characters and the corpus from 49.2k to 58.0k:
#: roughly 250 characters per acting tool, paid on every request whether or not the model
#: writes a sentence. Nothing here is deferrable — the property has to be on the schema of
#: whatever tool the model is about to call — and a shorter instruction was declined
#: because it gets narrations that describe the tool instead of the reason.
#:
#: **Raised a third time, from 33k, when the reads got it too.** The work log's collapsed
#: header now leads with the run's most recent reason, and a turn that spends ten calls
#: reading showed none for most of its length — a read's arguments say *what*, never *why*.
#: It took a fresh request to 35.6k characters and the corpus to 68.4k. The frontend could
#: have derived a phrase ("Reading agent.py") for free; that was declined because it restates
#: the arguments rather than giving the model's reason, which is the thing being paid for.
#:
#: **Raised a fourth time, from 38k, for `run_code`** (`agent/code_mode.py`): one more tool
#: whose description carries the Python signature of every tool the thread's level clears
#: unasked, since those stay direct calls *and* become callable from a script. It took a
#: fresh request from 36.6k to 46.0k characters at Auto. The library's own rendering — a
#: full docstring per function — would have cost 23.6k on its own; bare signatures, with the
#: prose left on the direct definition it repeats, is what brought it to 9.3k.
#:
#: **Raised a fifth time, from 48k, for one environment**: every thread has the shell as
#: well as the code tools, where a sandbox thread used to have the code tools alone. It
#: took a fresh request to 51.2k characters at Auto, net of the bash form of `code_execute`
#: it replaced.
CATALOG_CEILING_CHARS = 52_000

#: The same for the whole corpus — every dormant group revealed. Deferral moves a group's
#: cost from every turn to the turns that want it; it does not make the group free, and a
#: ceiling that only watched the fresh request would let the dormant half grow unwatched.
#: Raised from 50k, then from 60k, then from 72k, alongside the ceiling above and for the
#: same reasons; then from 82k for the dormant `agents` group (agents writing agents), and
#: from 84k for the dormant `routines` group, whose schedule is one compact string rather
#: than a nested object for that reason; then from 88k for the shell in every thread.
CORPUS_CEILING_CHARS = 92_000


def _dormant_categories() -> tuple[str, ...]:
    """The dormant declarations a real app assembles, read off the manifests the same way
    ``full_tool_categories`` reads their toolsets. Restating the set here would measure a
    deferral this file invented rather than the one the app performs."""
    return tuple(entry.category for manifest in discover_manifests() for entry in manifest.dormant)


async def _catalog(
    kind: WorkspaceKind, permission: PermissionLevel, *, scripts: bool = True
) -> tuple[tuple[int, int], tuple[int, int]]:
    """``((tools, chars) on a fresh request, (tools, chars) with every group revealed)`` for
    a real run in this workspace and at this level — resolved through the composed toolset stack
    the engine hands the Agent, and through the same ``effective_disabled_tools`` every run
    path fills ``RunDeps.disabled_tools`` from. Re-deriving the withheld set here would
    measure a narrowing this file invented rather than the one the app performs, and would
    stay green if the real path stopped applying the level at all.

    A dormant tool stays in the resolved toolset carrying ``defer_loading`` — that flag is
    what the model layer reads to keep the schema off the wire — so the fresh figure is the
    same subtraction the request performs, not a second opinion about it."""
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    # The level is deliberately absent here and rides on `RunDeps.permission` below: it is
    # applied live at the enabled gate, not folded into this set (`services/tool_policy`).
    # Nothing suspended: this file measures the catalog, not connectivity.
    all_online = stub_offline(frozenset())
    disabled = await effective_disabled_tools(SettingsStore(engine), all_online, "operator")
    run = Run(id="t", kind="chat", owner_id="operator", stream=RunStream())
    deps = RunDeps(
        run=run,
        owner_id="operator",
        disabled_tools=disabled,
        workspace_kind=kind,
        permission=permission,
    )
    ctx = RunContext(deps=deps, model=TestModel(), usage=RunUsage())
    stack = build_agent_toolsets(full_tool_categories(), dormant=_dormant_categories())[0]
    # Measured through the `run_code` wrapper every agent carries, since its description —
    # the signature of every tool the level clears — ships on every request too. Its catalog
    # is the fresh request's: a dormant group joins it only once revealed, so the corpus
    # figure below counts a revealed group's signatures once, as schemas, not twice.
    wrapper = code_mode_capability().get_wrapper_toolset(stack)
    assert wrapper is not None
    tools = await wrapper.get_tools(ctx)
    corpus = [tool.tool_def for name, tool in tools.items() if scripts or name != "run_code"]
    fresh = [tool_def for tool_def in corpus if not tool_def.defer_loading]
    return (
        (len(fresh), measure_overhead(None, [], fresh).tools),
        (len(corpus), measure_overhead(None, [], corpus).tools),
    )


async def _corpus_chars(kind: WorkspaceKind, permission: PermissionLevel) -> int:
    _, (_, chars) = await _catalog(kind, permission)
    return chars


async def test_a_fresh_request_stays_inside_its_ceiling():
    (count, chars), _ = await _catalog("sandbox", "auto")
    assert count > 0
    assert chars < CATALOG_CEILING_CHARS, (
        f"a fresh request's tool catalog now costs {chars} characters "
        f"(~{int(chars / CHARS_PER_TOKEN_JSON)} tokens) at the head of every request — "
        "either take something out, make its category dormant, or raise the ceiling on purpose"
    )


async def test_the_whole_corpus_stays_inside_its_ceiling():
    _, (count, chars) = await _catalog("sandbox", "auto")
    assert count > 0
    assert chars < CORPUS_CEILING_CHARS, (
        f"the tool corpus now costs {chars} characters "
        f"(~{int(chars / CHARS_PER_TOKEN_JSON)} tokens) once every dormant group is open — "
        "either take something out, or raise the ceiling on purpose"
    )


async def test_the_dormant_groups_are_most_of_what_a_turn_no_longer_pays():
    """The saving is the whole reason the groups are dormant, so it is pinned rather than
    left to be re-derived from two ceilings that move independently."""
    fresh, corpus = await _catalog("sandbox", "auto")
    assert fresh[1] < corpus[1] * 0.6, (
        f"a fresh request carries {fresh[1]} characters of the corpus' {corpus[1]} — "
        "deferral has stopped deferring most of what it used to"
    )


async def test_plan_hands_the_model_a_fraction_of_the_catalog():
    # Measured over the corpus: the narrowing is the permission level withholding tools
    # outright, and reading it off the fresh request would net it against deferral, which
    # withholds for an unrelated reason and hands everything back on request.
    for kind in WORKSPACE_TOOLS:
        acting = await _corpus_chars(kind, "auto")
        planning = await _corpus_chars(kind, "plan")
        assert planning < acting * 0.6, (
            f"{kind} at plan level costs {planning} characters against {acting} acting — "
            "the read-only narrowing has stopped narrowing"
        )


async def test_only_plan_narrows_the_catalog():
    # The other three levels decide *at the call*: taking a tool out of their catalog
    # would tell the model the capability does not exist rather than that it needs
    # permission, so their cost is identical by design — before and after deferral, which
    # is decided per category and knows nothing about the level.
    #
    # `run_code` is the one definition that does move with the level, and on purpose: its
    # description lists the tools the level clears unasked (`agent/code_mode.py`). So the
    # catalog is identical with it set aside, and the tool count is identical with it in.
    acting = {
        level: await _catalog("sandbox", level, scripts=False)
        for level in PERMISSION_LADDER
        if level != "plan"
    }
    assert len(set(acting.values())) == 1, acting
    counts: dict[PermissionLevel, tuple[int, int]] = {}
    for level in PERMISSION_LADDER:
        if level != "plan":
            fresh, corpus = await _catalog("sandbox", level)
            counts[level] = (fresh[0], corpus[0])
    assert len(set(counts.values())) == 1, counts


# --- and that a real request performs the subtraction these figures assume -------------


async def _measured_tools(*, dormant: Mapping[str, str], reveal: bool) -> int:
    """The schema cost a **real turn** records on its Run, in characters.

    Everything above computes the subtraction itself, from `defer_loading`, and then measures
    the list it produced. That is the right way to pin a ceiling and a blind spot for the
    claim underneath it: production does not perform this subtraction in the toolset stack, it
    performs it in the capability that reads the assembled request — and the two can disagree.
    They did. `ModelRequestParameters.function_tools` carries every definition the stack
    produced, dormant ones included, so measuring it reported a fresh request as costing the
    whole corpus, and the library's own `declared_function_tools` cannot be substituted
    blindly either (`agent/assembled.py`).
    """
    steps: list[object] = []

    async def stream_fn(
        _messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str | DeltaToolCalls]:
        steps.append(info)
        if reveal and len(steps) == 1:
            yield {0: DeltaToolCall(name="search_tools", json_args='{"queries": ["tasks"]}')}
        else:
            yield "done"

    agent = build_agent(
        FunctionModel(stream_function=stream_fn), categories=core_categories(), dormant=dormant
    )
    run = Run(id="t", kind="chat", owner_id="operator", stream=RunStream())
    async with agent.iter("go", deps=RunDeps(run=run, owner_id="operator")) as agent_run:
        await stream_agent_run(agent_run, run)
    assert run.context_overhead is not None, "no request was measured, so this asserted nothing"
    return run.context_overhead.tools


async def test_a_real_request_is_measured_without_the_schemas_it_withheld():
    everything = await _measured_tools(dormant=NO_DORMANT, reveal=False)
    withholding = await _measured_tools(dormant={"tasks": "the checklist"}, reveal=False)

    assert withholding < everything, (
        f"a turn withholding a category measured {withholding} characters of schema against "
        f"{everything} with the category offered — the gauge is charging the operator for "
        "schemas deferral kept off the wire"
    )


async def test_revealing_a_group_is_measured_as_costing_what_it_costs():
    """The other direction, and the one a naive fix gets wrong: `defer_loading` stays set
    after a reveal, so a measurement that trusts that flag alone goes on reporting the cheap
    figure for the rest of a turn that is paying the full one.

    Not asserted as *equal* to the up-front figure: a turn with anything dormant also carries
    `search_tools`, which a turn with nothing dormant has no reason to offer. So the revealed
    turn legitimately costs a little more than the same tools offered from the start, and the
    claim is a floor rather than an identity."""
    everything = await _measured_tools(dormant=NO_DORMANT, reveal=False)
    withholding = await _measured_tools(dormant={"tasks": "the checklist"}, reveal=False)
    revealed = await _measured_tools(dormant={"tasks": "the checklist"}, reveal=True)

    assert revealed > withholding, (
        f"a turn that revealed the withheld category still measured {revealed} characters "
        f"against {withholding} before the reveal — the gauge stopped noticing schemas the "
        "model asked for and is now on the wire"
    )
    assert revealed >= everything, (
        f"the revealed turn measured {revealed} characters against {everything} for the whole "
        "catalog offered up front, so some of what it revealed is going uncounted"
    )
