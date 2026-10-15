"""A revealed tool group, everywhere history is touched.

Dormant categories are loaded *by the model, mid-turn*, and the library remembers that in
one place only: the messages. Pydantic AI re-derives the revealed set from the outgoing
history on every single request, so a group stays loaded exactly as long as the exchange
that loaded it is still being replayed — and silently unloads the moment it isn't. Every
seam that rewrites, folds, branches or projects a history is therefore also a seam that
can take the browser back off a thread mid-task.

So these drive real turns through the real orchestrator over a ``FunctionModel``, and read
the **tool list each request was actually handed** rather than asserting about a toolset in
isolation: whether a group is loaded is a fact about the request that ships, not about the
stack that built it.

The one seam that needed fixing is compaction. Both folds replace the folded stretch with a
summary checkpoint, and the reveal goes with it — so the fold carries every tool the folded
stretch had revealed onto the checkpoint as a ``ToolAvailabilityDeltaPart``, the library's
own shape for "the tools changed here". Everything else here holds already, and is pinned
because nothing in the code says so.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import cast, override

from fastapi import FastAPI
from pydantic_ai import (
    FunctionToolset,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RunContext,
    ToolApproved,
    ToolAvailabilityDeltaPart,
    UserPromptPart,
)
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import (
    BaseToolCallPart,
    BaseToolReturnPart,
    RetryPromptPart,
    TextPart,
    ToolSearchCallPart,
    ToolSearchReturnPart,
)
from pydantic_ai.models import Model, ModelRequestParameters, StreamedResponse
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import RequestUsage

from agent import ParkedTurn, build_chat_orchestrator, build_resume_orchestrator
from agent.compaction_summary import summary_request
from agent.history import (
    TurnStart,
    drop_dangling_tool_calls,
    merge_consecutive_requests,
    revealed_tools,
    split_injected_requests,
)
from agent.summarize import AutoCompactPolicy, compact_conversation
from routes.deps import OPERATOR_ID, app_state
from runs import Run, RunStatus, TurnOverhead
from services.conversations import ConversationStore
from tools import RunDeps

from ._helpers import client_app, fold_with, is_fold_request
from ._json import arr, obj, parse_json, s

#: The dormant group under test. One plain tool and one that needs approval, so the same
#: group covers both "the model may call it once it is loaded" and the park.
DORMANT = {"browse": "drive a real browser"}

REVEALED = {"browse_open_page", "browse_delete_page"}

#: Measured, and empty — these threads run against a deliberately tiny window, and an
#: unmeasured overhead would fold every one of them on sight.
_NO_OVERHEAD = TurnOverhead(system=0, tools=0)

#: A fold keeps nothing verbatim, so the checkpoint is the whole of what the retried
#: request replays.
_FOLD_ALL = AutoCompactPolicy(enabled=True, threshold=0.80)


def _categories() -> dict[str, FunctionToolset[RunDeps]]:
    browse: FunctionToolset[RunDeps] = FunctionToolset()

    @browse.tool_plain
    def open_page(url: str) -> str:
        return f"opened {url}"

    @browse.tool_plain(requires_approval=True)
    def delete_page(url: str) -> str:
        return f"deleted {url}"

    plain: FunctionToolset[RunDeps] = FunctionToolset()

    @plain.tool_plain
    def now() -> str:
        return "noon"

    return {"browse": browse, "builtin": plain}


# --- models that read the tool list they were handed ----------------------------------


def _called(messages: Sequence[ModelMessage], name: str) -> bool:
    return any(
        isinstance(part, BaseToolCallPart | BaseToolReturnPart | RetryPromptPart)
        and part.tool_name == name
        for message in messages
        for part in message.parts
    )


def _delta(name: str, args: dict[str, object]) -> DeltaToolCalls:
    return {0: DeltaToolCall(name=name, json_args=json.dumps(args))}


def _searcher(seen: list[set[str]], *, then_call: str | None = None) -> FunctionModel:
    """Searches for the browser on its first step, optionally calls one of its tools, then
    answers — recording the tool list of every request on the way."""

    async def stream_fn(
        messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str | DeltaToolCalls]:
        seen.append({tool.name for tool in info.function_tools})
        if not _called(messages, "search_tools"):
            yield _delta("search_tools", {"queries": ["browse"]})
        elif then_call and not _called(messages, then_call):
            yield _delta(then_call, {"url": "https://example.test"})
        else:
            yield "done"

    return FunctionModel(stream_function=stream_fn)


def _answerer(seen: list[set[str]]) -> FunctionModel:
    """Answers without ever searching, so what it was handed is the whole story — and
    writes ``FOLDED AWAY`` when asked to fold, a request it does not record, since ``seen``
    is about what the *turn* was offered."""

    async def stream_fn(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        if is_fold_request(messages):
            yield "FOLDED AWAY"
            return
        seen.append({tool.name for tool in info.function_tools})
        yield "done"

    return FunctionModel(stream_function=stream_fn)


def _ctx_error() -> ModelHTTPError:
    return ModelHTTPError(
        status_code=400,
        model_name="m",
        body={"error": {"code": "context_length_exceeded", "message": "too long"}},
    )


class _OverflowsThenAnswers(WrapperModel):
    """Refuses its first request as over-long, then answers — so the recorded tool list is
    the *retried* request's, taken after the mid-turn fold rebuilt the history."""

    def __init__(self, seen: list[set[str]]) -> None:
        super().__init__(_answerer(seen))
        self.refused: bool = False

    def _check(self, messages: list[ModelMessage]) -> None:
        if not self.refused and not is_fold_request(messages):
            self.refused = True
            raise _ctx_error()

    @override
    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        self._check(messages)
        return await super().request(messages, model_settings, model_request_parameters)

    @override
    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: RunContext[object] | None = None,
    ) -> AsyncGenerator[StreamedResponse]:
        self._check(messages)
        async with super().request_stream(
            messages, model_settings, model_request_parameters, run_context
        ) as stream:
            yield stream


# --- driving a turn -------------------------------------------------------------------


def _discovered(result: object) -> set[str]:
    body = obj(parse_json(json.dumps(result)))
    return {s(obj(match)["name"]) for match in arr(body["discovered_tools"])}


def _store(app: FastAPI) -> ConversationStore:
    return app_state(app).conversations


def _run(
    app: FastAPI,
    cid: str,
    *,
    model: Model,
    prompt: str | None = "open a page",
    context_window: int | None = None,
    auto_compact: AutoCompactPolicy | None = None,
) -> Run:
    orch = build_chat_orchestrator(
        prompt,
        model=model,
        categories=_categories(),
        dormant=DORMANT,
        store=_store(app),
        conversation_id=cid,
        context_window=context_window,
        auto_compact=auto_compact,
    )
    return app_state(app).runs.submit(kind="chat", owner_id=OPERATOR_ID, orchestrator=orch)


async def _turn(
    app: FastAPI,
    cid: str,
    *,
    model: Model,
    prompt: str | None = "open a page",
    context_window: int | None = None,
    auto_compact: AutoCompactPolicy | None = None,
) -> Run:
    run = _run(
        app,
        cid,
        model=model,
        prompt=prompt,
        context_window=context_window,
        auto_compact=auto_compact,
    )
    _ = await run.wait()
    return run


def _browse(names: set[str]) -> set[str]:
    return {name for name in names if name.startswith("browse_")}


def _texts(messages: Sequence[ModelMessage]) -> list[str]:
    contents = (
        cast(object, getattr(part, "content", None))
        for message in messages
        for part in message.parts
    )
    return [content for content in contents if isinstance(content, str)]


def _seed_revealed_turn(store: ConversationStore, cid: str, *, input_tokens: int = 100) -> None:
    """One recorded exchange in which the model loaded the browser — the shape the engine
    really produces (pinned end to end by the persistence tests below), recorded directly
    so a fold test can start from a thread that has already revealed something."""
    _ = store.record(
        cid,
        [
            ModelRequest(parts=[UserPromptPart(content="open a page")]),
            ModelResponse(
                parts=[ToolSearchCallPart(args={"queries": ["browse"]}, tool_call_id="s")]
            ),
            ModelRequest(
                parts=[
                    ToolSearchReturnPart(
                        tool_call_id="s",
                        content={"discovered_tools": [{"name": name} for name in sorted(REVEALED)]},
                    )
                ]
            ),
            ModelResponse(
                parts=[TextPart(content="opened it")],
                usage=RequestUsage(input_tokens=input_tokens, output_tokens=10),
            ),
        ],
    )


# --- 1. persistence and replay --------------------------------------------------------


async def test_the_search_exchange_survives_the_store_and_the_next_turn_needs_no_repeat():
    async with client_app() as (_client, app):
        store = _store(app)
        cid = await store.create_conversation(OPERATOR_ID)
        first: list[set[str]] = []
        _ = await _turn(app, cid, model=_searcher(first))

        # The turn itself: nothing on the first request, the whole group on the second.
        assert _browse(first[0]) == set()
        assert "search_tools" in first[0]
        assert _browse(first[1]) == REVEALED

        # The library's typed parts are what the store kept — cold, through the vault.
        await store.flush()
        store.evict_cache()
        cold = await store.history(cid)
        assert any(isinstance(p, ToolSearchCallPart) for m in cold for p in m.parts)
        assert any(isinstance(p, ToolSearchReturnPart) for m in cold for p in m.parts)

        # And the next turn opens with the group already loaded, so a model that would
        # have searched again never needs to.
        second: list[set[str]] = []
        _ = await _turn(app, cid, model=_answerer(second), prompt="and again")
        assert _browse(second[0]) == REVEALED


async def test_the_operator_message_is_recorded_once_across_a_search_turn():
    """The persistence index is measured against a history the library also rewrites. A
    search turn adds a response and a tool-return request in the middle of it, which is
    exactly where an off-by-one drops the operator's own message or records it twice."""
    async with client_app() as (_client, app):
        store = _store(app)
        cid = await store.create_conversation(OPERATOR_ID)
        _ = await _turn(app, cid, model=_searcher([]))
        await store.flush()
        store.evict_cache()

        assert _texts(await store.history(cid)).count("open a page") == 1
        view = await store.messages_view(cid)
        assert [m.role for m in view] == ["user", "assistant"]


async def test_the_history_surgeries_leave_a_reveal_where_it_was():
    """The three rewrites every turn runs its replay through, against the parts a search
    leaves behind. A dropped or displaced return part is a silently unloaded group."""
    call = ToolSearchCallPart(args={"queries": ["browse"]}, tool_call_id="s")
    ret = ToolSearchReturnPart(
        tool_call_id="s", content={"discovered_tools": [{"name": "browse_open_page"}]}
    )
    delta = ToolAvailabilityDeltaPart(tools_added=["browse_delete_page"])

    # A turn stopped at a bound after the call but before the result: the call is dropped
    # (replaying it would be a provider error) and nothing was revealed to lose.
    stopped = [ModelRequest(parts=[UserPromptPart(content="hi")]), ModelResponse(parts=[call])]
    assert len(drop_dangling_tool_calls(stopped)) == 1

    # A mid-run steering message split out of the return request leaves the return — and
    # the delta beside it — on the request that carries them.
    mixed = ModelRequest(parts=[ret, delta, UserPromptPart(content="also do this")])
    split = split_injected_requests([mixed])
    assert [type(p).__name__ for p in split[0].parts] == [
        "ToolSearchReturnPart",
        "ToolAvailabilityDeltaPart",
    ]
    assert isinstance(split[1].parts[0], UserPromptPart)
    assert revealed_tools(split) == ("browse_open_page", "browse_delete_page")

    # A checkpoint's carried delta merged into the turn that follows it stays on the
    # history's side of the boundary: `parts` counts the turn's own from the end.
    merged = merge_consecutive_requests(
        [
            ModelRequest(parts=[delta, UserPromptPart(content="SUMMARY")]),
            ModelRequest(parts=[UserPromptPart(content="next")]),
        ]
    )
    assert len(merged) == 1
    assert _texts(TurnStart(0, 1).slice(merged)) == ["next"]
    assert revealed_tools(merged) == ("browse_delete_page",)


# --- 2. projection and transcript -----------------------------------------------------


async def test_the_work_log_renders_the_search_as_a_tool_row():
    async with client_app() as (_client, app):
        store = _store(app)
        cid = await store.create_conversation(OPERATOR_ID)
        _ = await _turn(app, cid, model=_searcher([]))
        await store.flush()
        store.evict_cache()

        view = await store.messages_view(cid)
        tools = view[1].tools
        assert [(t.name, t.status) for t in tools] == [("search_tools", "ok")]
        assert _discovered(tools[0].result) == REVEALED
        # The answer is still the answer — the search is a row, not the bubble.
        assert view[1].content == "done"


async def test_the_summary_request_keeps_the_search_so_its_tools_are_the_turns():
    """The summary is the thread's own model continuing its conversation, and a reveal
    lives only in the messages — so the summary request replays the search exactly as a
    turn would, and is offered the same loaded group. Dropping it would change the tool
    array and throw away the very prefix the summary is written on to reuse."""
    async with client_app() as (_client, app):
        store = _store(app)
        cid = await store.create_conversation(OPERATOR_ID)
        _ = await _turn(app, cid, model=_searcher([]))

        request = summary_request(await store.model_history(cid))
        assert set(revealed_tools(request)) == REVEALED
        assert any(isinstance(p, ToolSearchCallPart) for m in request for p in m.parts)


# --- 3. streaming ---------------------------------------------------------------------


async def test_the_search_streams_as_a_tool_call():
    """The frontend has no other way to know the turn stopped to load something."""
    async with client_app() as (_client, app):
        store = _store(app)
        cid = await store.create_conversation(OPERATOR_ID)
        run = await _turn(app, cid, model=_searcher([]))

        bodies = [e.body for e in run.stream.replay()]
        started = [b for b in bodies if b.type == "tool.started"]
        completed = [b for b in bodies if b.type == "tool.completed"]
        assert [b.name for b in started] == ["search_tools"]
        assert started[0].args == {"queries": ["browse"]}
        assert [b.name for b in completed] == ["search_tools"]
        assert _discovered(completed[0].result) == REVEALED


# --- 4. branching ---------------------------------------------------------------------


async def test_a_reveal_rides_the_branch_that_contains_it():
    """Two versions of the same turn, one of which loaded the browser. Which tools the
    next turn is offered has to follow the version the operator is actually on."""
    async with client_app() as (_client, app):
        store = _store(app)
        cid = await store.create_conversation(OPERATOR_ID)
        _ = await _turn(app, cid, model=_searcher([]))

        # Regenerate that answer without searching: the new version carries no reveal.
        assistant_id = (await store.messages_view(cid))[1].id
        assert await store.regenerate_point(cid, assistant_id)
        _ = await _turn(app, cid, model=_answerer([]), prompt=None)

        plain: list[set[str]] = []
        _ = await _turn(app, cid, model=_answerer(plain), prompt="and now")
        assert _browse(plain[0]) == set()

        # Switch back to the version that searched, and the group is offered again.
        newest_id = (await store.messages_view(cid))[1].id
        assert await store.switch_version(cid, newest_id, 0)
        restored: list[set[str]] = []
        _ = await _turn(app, cid, model=_answerer(restored), prompt="and now")
        assert _browse(restored[0]) == REVEALED


async def test_an_edit_above_the_reveal_leaves_it_off_the_branch():
    async with client_app() as (_client, app):
        store = _store(app)
        cid = await store.create_conversation(OPERATOR_ID)
        _ = await _turn(app, cid, model=_searcher([]))

        user_id = (await store.messages_view(cid))[0].id
        assert await store.edit_point(cid, user_id)
        edited: list[set[str]] = []
        _ = await _turn(app, cid, model=_answerer(edited), prompt="something else entirely")
        assert _browse(edited[0]) == set()


async def test_a_rewind_past_the_reveal_leaves_it_off_the_branch():
    async with client_app() as (_client, app):
        store = _store(app)
        cid = await store.create_conversation(OPERATOR_ID)
        _ = await _turn(app, cid, model=_searcher([]))
        _ = await _turn(app, cid, model=_answerer([]), prompt="and again")

        # Back to the operator's first message: everything that revealed the group hangs
        # off the branch this leaves behind.
        user_id = (await store.messages_view(cid))[0].id
        assert await store.rewind(cid, user_id)
        after: list[set[str]] = []
        _ = await _turn(app, cid, model=_answerer(after), prompt="try again")
        assert _browse(after[0]) == set()


# --- 5. park and resume ---------------------------------------------------------------


async def test_a_reveal_before_a_park_is_still_in_effect_after_the_resume():
    """A park stashes the agent and the history; nothing on the resume path rebuilds the
    toolset, and the reveal is read back off that same history. A resume that lost it would
    approve a call to a tool the model can no longer see."""
    async with client_app() as (_client, app):
        store = _store(app)
        cid = await store.create_conversation(OPERATOR_ID)
        seen: list[set[str]] = []
        run = await _turn(app, cid, model=_searcher(seen, then_call="browse_delete_page"))

        assert run.status is RunStatus.awaiting_input
        parked = run.parked_payload
        assert isinstance(parked, ParkedTurn)
        assert set(revealed_tools(parked.message_history)) == REVEALED
        call_id = parked.requests.approvals[0].tool_call_id

        _ = await app_state(app).runs.resume(
            run.id, build_resume_orchestrator(parked, {call_id: ToolApproved()}, store=store)
        )
        _ = await run.wait()

        assert run.status is RunStatus.done
        # The request that continued the turn was still offered the whole group.
        assert _browse(seen[-1]) == REVEALED
        completed = [
            b.name for b in (e.body for e in run.stream.replay()) if b.type == "tool.completed"
        ]
        assert "browse_delete_page" in completed


# --- 6. a fold must not silently un-reveal --------------------------------------------


async def test_the_prelude_fold_carries_the_reveal_onto_the_checkpoint():
    async with client_app() as (_client, app):
        store = _store(app)
        cid = await store.create_conversation(OPERATOR_ID)
        _seed_revealed_turn(store, cid, input_tokens=7_000)
        await store.set_overhead(cid, _NO_OVERHEAD)

        seen: list[set[str]] = []
        run = await _turn(
            app,
            cid,
            model=_answerer(seen),
            prompt="x" * 6_000,
            context_window=10_000,
            auto_compact=_FOLD_ALL,
        )

        assert run.status is RunStatus.done
        bodies = (e.body for e in run.stream.replay())
        reasons = [b.reason for b in bodies if b.type == "conversation.compacted"]
        assert reasons == ["threshold"]
        # The exchange that loaded the browser is gone from the replay, and the group is
        # still on the wire.
        replayed = await store.model_history(cid)
        assert not any(isinstance(p, ToolSearchReturnPart) for m in replayed for p in m.parts)
        assert _browse(seen[0]) == REVEALED


async def test_a_fold_of_a_thread_that_revealed_nothing_reveals_nothing():
    """The control the test above needs: the carry is what the folded stretch actually
    showed the model, not a standing re-offer of every dormant group."""
    async with client_app() as (_client, app):
        store = _store(app)
        cid = await store.create_conversation(OPERATOR_ID)
        _ = store.record(
            cid,
            [
                ModelRequest(parts=[UserPromptPart(content="hello")]),
                ModelResponse(
                    parts=[TextPart(content="hi")],
                    usage=RequestUsage(input_tokens=7_000, output_tokens=10),
                ),
            ],
        )
        await store.set_overhead(cid, _NO_OVERHEAD)

        seen: list[set[str]] = []
        _ = await _turn(
            app,
            cid,
            model=_answerer(seen),
            prompt="x" * 6_000,
            context_window=10_000,
            auto_compact=_FOLD_ALL,
        )
        assert _browse(seen[0]) == set()


async def test_the_overflow_fold_keeps_the_tools_the_turn_had_loaded():
    """The mid-turn fold rebuilds the replay under a turn already in flight and re-sends
    the request that overran. Losing the reveal there would answer the retry with a
    strictly smaller toolset than the one the model was working with."""
    async with client_app() as (_client, app):
        store = _store(app)
        cid = await store.create_conversation(OPERATOR_ID)
        _seed_revealed_turn(store, cid)
        await store.set_overhead(cid, _NO_OVERHEAD)
        before = _texts(await store.history(cid))

        seen: list[set[str]] = []
        run = await _turn(
            app,
            cid,
            model=_OverflowsThenAnswers(seen),
            prompt="next question",
            context_window=10_000,
            auto_compact=_FOLD_ALL,
        )

        assert run.status is RunStatus.done
        assert _browse(seen[-1]) == REVEALED
        # And the checkpoint riding a delta part did not confuse the turn boundary: the
        # summary and the operator's prompt are each recorded exactly once.
        await store.flush()
        store.evict_cache()
        after = _texts(await store.history(cid))
        assert len(after) == len(before) + 3
        assert sum("FOLDED AWAY" in text for text in after) == 1
        assert after.count("next question") == 1


async def test_the_carried_reveal_persists_with_the_checkpoint():
    """It has to survive the vault, not just the turn that folded: a cold reload rebuilds
    the replay from the stored blobs, and a delta that lived only in memory would unload
    the group on the next backend restart."""
    async with client_app() as (_client, app):
        store = _store(app)
        cid = await store.create_conversation(OPERATOR_ID)
        _seed_revealed_turn(store, cid)

        outcome = await compact_conversation(
            store, cid, reason="manual", **fold_with("so far", categories=_categories())
        )
        assert outcome is not None
        await store.flush()
        store.evict_cache()

        replayed = await store.model_history(cid)
        assert revealed_tools(replayed) == tuple(sorted(REVEALED))
        # The divider still reads as the summary — the delta is bookkeeping the operator
        # never sees, and it leads the checkpoint's parts.
        checkpoint = next(m for m in replayed if isinstance(m, ModelRequest))
        assert isinstance(checkpoint.parts[0], ToolAvailabilityDeltaPart)
        view = await store.messages_view(cid)
        divider = next(m for m in view if m.role == "compaction")
        assert divider.content.endswith("so far")
        assert divider.compaction_reason == "manual"


async def test_a_second_fold_inherits_the_first_folds_reveal():
    """A checkpoint is folded like any other message, so its carried delta has to be read
    as evidence in its own right — otherwise a reveal survives exactly one compaction."""
    async with client_app() as (_client, app):
        store = _store(app)
        cid = await store.create_conversation(OPERATOR_ID)
        _seed_revealed_turn(store, cid)

        fold = fold_with("so far", categories=_categories())
        assert await compact_conversation(store, cid, reason="manual", **fold)
        _ = store.record(
            cid,
            [
                ModelRequest(parts=[UserPromptPart(content="carry on")]),
                ModelResponse(parts=[TextPart(content="carried")]),
            ],
        )
        assert await compact_conversation(store, cid, reason="manual", **fold)

        replayed = await store.model_history(cid)
        assert len(replayed) == 1  # the second checkpoint absorbed the first
        assert revealed_tools(replayed) == tuple(sorted(REVEALED))


async def test_a_fold_records_no_delta_when_nothing_was_revealed():
    """A part with an empty ``tools_added`` is a claim about the wire — a provider beta
    header, an announcement in system voice — for a change that never happened."""
    async with client_app() as (_client, app):
        store = _store(app)
        cid = await store.create_conversation(OPERATOR_ID)
        _ = store.record(
            cid,
            [
                ModelRequest(parts=[UserPromptPart(content="hello")]),
                ModelResponse(parts=[TextPart(content="hi")]),
            ],
        )
        assert await compact_conversation(store, cid, reason="manual", **fold_with("so far"))
        parts = [p for m in await store.model_history(cid) for p in m.parts]
        assert [type(p).__name__ for p in parts] == ["UserPromptPart"]
