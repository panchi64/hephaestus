"""The per-conversation work summary: what the summariser reads, and what the sweep picks.

Two halves, and they fail in different ways.

The **transcript** is the half with a security property. Unlike the titler next door, this
summariser has to read model-authored text — what the agent did is only in its own answers
and tool calls — so the operator's turns stand in the clear and everything else is fenced.
A regression there does not break the feature; it quietly hands a small, unguarded model a
web page's instructions wearing the workspace's voice, and stores the result as the
thread's own account of itself. Tool *returns* are excluded outright, and that exclusion is
load-bearing for the same reason.

The **sweep** is the half with an economic property: every candidate it picks is a model
call, so each of the three conditions it applies is tested from both sides. A thread still
in use, a thread already summarized since it last spoke, and a thread with a run streaming
into it are all threads where the call would be wasted or wrong.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import final, override

import pytest
from pydantic_ai import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.messages import ToolReturnPart
from pydantic_ai.models import Model
from pydantic_ai.models.function import AgentInfo, FunctionModel

from agent.work_summary import MAX_SUMMARY_LEN, clean_summary, summarize_work, work_transcript
from core.config import Settings, get_settings
from core.db import init_db, make_engine
from core.vault import Vault
from runs import Run, RunRegistry
from services import work_summaries as work_summaries_module
from services.conversations import ConversationStore
from services.registry import ResolvedModel
from services.settings_store import (
    WORK_SUMMARY_IDLE_MINUTES_KEY,
    WORK_SUMMARY_IDLE_MINUTES_MAX,
    SettingsStore,
    get_work_summary_idle_minutes,
    set_work_summary_idle_minutes,
)
from services.work_summaries import BackgroundModels, WorkSummaryService

from ._helpers import client_app
from ._json import json_body, n

OWNER = "op"


# --- fixtures ---------------------------------------------------------------


def _turn(prompt: str, answer: str) -> list[ModelMessage]:
    return [
        ModelRequest(parts=[UserPromptPart(content=prompt)]),
        ModelResponse(parts=[TextPart(content=answer)]),
    ]


def _tool_turn(prompt: str, *, narration: str | None, result: str) -> list[ModelMessage]:
    args: dict[str, str] = {"path": "notes.txt"}
    if narration is not None:
        args["narration"] = narration
    return [
        ModelRequest(parts=[UserPromptPart(content=prompt)]),
        ModelResponse(parts=[ToolCallPart(tool_name="files_read", args=args, tool_call_id="1")]),
        ModelRequest(
            parts=[ToolReturnPart(tool_name="files_read", content=result, tool_call_id="1")]
        ),
        ModelResponse(parts=[TextPart(content="read it")]),
    ]


def _fenced_regions(rendered: str) -> tuple[list[str], list[str]]:
    """The rendered transcript split into (clear, fenced) lines, read off the markers the
    way the model will read them."""
    clear: list[str] = []
    fenced: list[str] = []
    inside = False
    for line in rendered.splitlines():
        if line.startswith("[BEGIN UNTRUSTED CONTENT "):
            inside = True
        elif line.startswith("[END UNTRUSTED CONTENT "):
            inside = False
        else:
            (fenced if inside else clear).append(line)
    return clear, fenced


def _replies(text: str) -> tuple[FunctionModel, list[str]]:
    """A utility model answering every call with ``text``, recording what it was asked."""
    seen: list[str] = []

    async def respond(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        seen.append(
            "\n".join(
                part.content
                for message in messages
                if isinstance(message, ModelRequest)
                for part in message.parts
                if isinstance(part, UserPromptPart) and isinstance(part.content, str)
            )
        )
        return ModelResponse(parts=[TextPart(content=text)])

    return FunctionModel(respond), seen


def _raises() -> FunctionModel:
    async def respond(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        raise RuntimeError("the endpoint is down")

    return FunctionModel(respond)


# --- the transcript ---------------------------------------------------------


class TestTheTranscript:
    def test_the_operator_is_clear_and_everything_else_is_fenced(self):
        """The one voice in the thread that is the operator's own stays outside the fence;
        the model's prose and its tool calls are text an injected page can reach, so they go
        inside it."""
        rendered = work_transcript(_turn("ship the fix", "Rewrote services/foo.py and ran it."))
        clear, fenced = _fenced_regions(rendered)
        assert any("ship the fix" in line for line in clear)
        assert not any("Rewrote services/foo.py" in line for line in clear)
        assert any("Rewrote services/foo.py" in line for line in fenced)

    def test_the_preamble_names_the_nonce_the_fences_carry(self):
        """One preamble for the whole transcript, and the token it names has to be the one
        on the markers — a fence whose token the preamble never announced is a fence the
        model has no reason to respect."""
        rendered = work_transcript(_turn("go", "done"))
        first, _, rest = rendered.partition("\n")
        nonce = first.split("tagged ")[1].split(".")[0].strip()
        assert f"[BEGIN UNTRUSTED CONTENT {nonce}]" in rest
        assert f"[END UNTRUSTED CONTENT {nonce}]" in rest

    def test_a_tool_call_carries_its_namespaced_name_and_its_narration(self):
        """Narration is the model's own one-sentence reason for the call, and it is the
        whole reason returns can be left out — without it a call is a bare verb."""
        rendered = work_transcript(
            _tool_turn("read it", narration="Checking what the notes file already says", result="x")
        )
        _, fenced = _fenced_regions(rendered)
        body = "\n".join(fenced)
        assert "files_read" in body
        assert "Checking what the notes file already says" in body

    def test_a_call_without_narration_is_still_named(self):
        """A read-class tool is never offered the property, so its call has no sentence —
        the name alone still belongs in the account of what the thread did."""
        rendered = work_transcript(_tool_turn("read it", narration=None, result="x"))
        assert "files_read" in rendered

    def test_tool_returns_are_excluded_entirely(self):
        """The bulkiest and most injectable part of a thread, and redundant here: the
        narration says why the call was made and the answer says what came of it."""
        rendered = work_transcript(
            _tool_turn("read it", narration="peek", result="SECRET-RETURN-PAYLOAD")
        )
        assert "SECRET-RETURN-PAYLOAD" not in rendered

    def test_an_empty_history_renders_nothing(self):
        assert work_transcript([]) == ""
        assert work_transcript([ModelRequest(parts=[UserPromptPart(content="  ")])]) == ""

    def test_the_excerpt_budget_drops_the_oldest_blocks(self):
        """Over budget, the opening of a thread goes and the end stays — where the work
        landed is the half a returning operator cannot reconstruct from the title."""
        history: list[ModelMessage] = []
        for index in range(40):
            history.extend(_turn(f"request number {index} " + "x" * 200, f"answer {index}"))
        rendered = work_transcript(history, excerpt=2000)
        assert len(rendered) <= 2000
        assert "answer 39" in rendered
        assert "request number 0 " not in rendered


# --- the output cap ---------------------------------------------------------


class TestTheOutputCap:
    def test_a_short_summary_is_stored_unchanged(self):
        assert (
            clean_summary("Fixed the import cycle in app.py.")
            == "Fixed the import cycle in app.py."
        )

    def test_an_over_long_summary_is_cut_at_a_word_boundary(self):
        raw = "word " * 200
        cleaned = clean_summary(raw)
        assert cleaned is not None
        assert len(cleaned) <= MAX_SUMMARY_LEN
        # The cut lands between words: nothing but the ellipsis follows the last full one.
        assert cleaned.endswith("word…")

    def test_a_leaked_think_block_never_becomes_the_summary(self):
        assert clean_summary(
            "<think>weighing it up</think>\nRan the suite; two cases still fail."
        ) == ("Ran the suite; two cases still fail.")

    def test_an_empty_reply_is_no_summary_rather_than_an_empty_one(self):
        assert clean_summary("   ") is None
        assert clean_summary("<think>only reasoning</think>") is None


# --- the model call ---------------------------------------------------------


class TestTheModelCall:
    async def test_it_summarizes_from_the_fenced_transcript(self):
        model, seen = _replies("Rewrote the parser and got the suite green.")
        summary = await summarize_work(model, _turn("fix the parser", "Rewrote it."))
        assert summary == "Rewrote the parser and got the suite green."
        assert "UNTRUSTED CONTENT" in seen[0]

    async def test_a_model_failure_degrades_to_none(self):
        """Best-effort: the band is a convenience, and a dead endpoint must not surface as
        an exception in a background sweep."""
        assert await summarize_work(_raises(), _turn("go", "done")) is None

    async def test_an_empty_thread_asks_no_model_at_all(self):
        model, seen = _replies("never called")
        assert await summarize_work(model, []) is None
        assert seen == []


# --- the sweep --------------------------------------------------------------


async def _store(tmp_path: Path) -> ConversationStore:
    vault = Vault(tmp_path / "keyfile.json")
    await vault.setup("pw")
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    store = ConversationStore(engine, vault)
    await store.start()
    return store


@final
class _Registry(BackgroundModels):
    """The one registry method the sweep calls, answering ``model``."""

    def __init__(self, model: Model) -> None:
        self._model: Model = model

    @override
    async def resolve_background(
        self,
        *,
        owner_id: str,
        override_endpoint_id: str | None = None,
        override_model: str | None = None,
    ) -> ResolvedModel:
        return ResolvedModel(model=self._model, reasoning_off={}, context_window=128_000)


async def _seeded(store: ConversationStore, *, messages: int, age_minutes: float) -> str:
    """A conversation with ``messages`` rows whose ``updated_at`` sits ``age_minutes`` in
    the past — the two facts every candidate condition is read off.

    The write-behind drainer is joined before the clock is set back, because the candidate
    query counts *rows* and the persist itself bumps ``updated_at``."""
    conversation_id = await store.create_conversation(OWNER)
    history: list[ModelMessage] = []
    for index in range(messages // 2 + messages % 2):
        history.extend(_turn(f"ask {index}", f"answer {index}"))
    _ = store.record(conversation_id, history[:messages])
    await store.flush()
    _stamp(store, conversation_id, datetime.now(UTC) - timedelta(minutes=age_minutes))
    return conversation_id


def _stamp(
    store: ConversationStore,
    conversation_id: str,
    when: datetime,
    *,
    summarized_at: datetime | None = None,
) -> None:
    """Age a conversation by hand. The sweep's whole trigger is a clock comparison, so the
    alternative is a test that sleeps for the idle window. ``summarized_at`` moves the other
    side of the staleness comparison, for the case where a thread spoke *after* it was last
    summarized."""
    from sqlmodel import Session

    from models.conversation import Conversation

    with Session(store.engine) as session:
        conversation = session.get(Conversation, conversation_id)
        assert conversation is not None
        conversation.updated_at = when
        if summarized_at is not None:
            conversation.work_summary_at = summarized_at
        session.add(conversation)
        session.commit()


def _service(
    store: ConversationStore,
    model: Model,
    runs: RunRegistry | None = None,
    settings_store: SettingsStore | None = None,
) -> WorkSummaryService:
    if settings_store is None:
        settings_engine = make_engine("sqlite:///:memory:")
        init_db(settings_engine)
        settings_store = SettingsStore(settings_engine)
    return WorkSummaryService(
        store=store,
        settings_store=settings_store,
        models=_Registry(model),
        runs=runs or RunRegistry(),
        vault=store.vault,
        settings=Settings(),
        owner_id=OWNER,
        # The real summarizer over a scripted model, so a sweep test exercises the whole
        # path — the seam exists for the layering, not to stub the interesting part out.
        summarize=summarize_work,
    )


class TestTheSweep:
    async def test_a_fresh_conversation_is_not_a_candidate(self, tmp_path: Path):
        """A thread the operator is still working in would be summarized mid-sentence, and
        the summary would be stale before the next turn lands."""
        store = await _store(tmp_path)
        _ = await _seeded(store, messages=4, age_minutes=1)
        model, seen = _replies("never called")
        assert await _service(store, model).sweep() == 0
        assert seen == []

    async def test_an_idle_conversation_with_a_stale_summary_is_summarized(self, tmp_path: Path):
        store = await _store(tmp_path)
        conversation_id = await _seeded(store, messages=4, age_minutes=60)
        model, _ = _replies("Answered four questions about the parser.")
        assert await _service(store, model).sweep() == 1
        summary = await store.get_summary(conversation_id, OWNER)
        assert summary is not None
        assert summary.work_summary == "Answered four questions about the parser."

    async def test_a_thread_already_summarized_since_its_last_message_is_skipped(
        self, tmp_path: Path
    ):
        """The staleness rule is what keeps an idle workspace from costing a model call per
        thread per minute forever."""
        store = await _store(tmp_path)
        _ = await _seeded(store, messages=4, age_minutes=60)
        model, seen = _replies("Answered four questions.")
        service = _service(store, model)
        assert await service.sweep() == 1
        assert await service.sweep() == 0
        assert len(seen) == 1

    async def test_a_thread_that_spoke_again_becomes_a_candidate_again(self, tmp_path: Path):
        """The mirror of the test above: staleness is a comparison, not a flag, so a thread
        that moved after being summarized is stale again with nothing to reset."""
        store = await _store(tmp_path)
        conversation_id = await _seeded(store, messages=4, age_minutes=60)
        model, _ = _replies("first account")
        service = _service(store, model)
        assert await service.sweep() == 1
        # Summarized two hours ago, spoke one hour ago, quiet since: stale and idle at once.
        now = datetime.now(UTC)
        _stamp(
            store,
            conversation_id,
            now - timedelta(minutes=60),
            summarized_at=now - timedelta(minutes=120),
        )
        assert await service.sweep() == 1

    async def test_a_conversation_with_a_live_run_is_skipped(self, tmp_path: Path):
        """Idle is measured off ``updated_at``, which an in-flight turn has not moved — so
        without this check a streaming answer is summarized as though it had not happened."""
        store = await _store(tmp_path)
        conversation_id = await _seeded(store, messages=4, age_minutes=60)
        runs = RunRegistry()
        started, release = asyncio.Event(), asyncio.Event()

        async def orch(_run: Run) -> None:
            started.set()
            _ = await release.wait()

        run = runs.submit(
            kind="chat", owner_id=OWNER, orchestrator=orch, conversation_id=conversation_id
        )
        _ = await started.wait()
        model, seen = _replies("never called")
        assert await _service(store, model, runs).sweep() == 0
        assert seen == []
        release.set()
        _ = await run.wait()

    async def test_a_thread_with_one_message_has_no_work_to_account_for(self, tmp_path: Path):
        store = await _store(tmp_path)
        _ = await _seeded(store, messages=1, age_minutes=60)
        model, seen = _replies("never called")
        assert await _service(store, model).sweep() == 0
        assert seen == []

    async def test_a_locked_vault_sweeps_nothing(self, tmp_path: Path):
        """No key means nothing to decrypt the history with and nothing to seal the summary
        under — the pass returns rather than raising its way through a decrypt."""
        store = await _store(tmp_path)
        _ = await _seeded(store, messages=4, age_minutes=60)
        store.vault.lock()
        model, seen = _replies("never called")
        assert await _service(store, model).sweep() == 0
        assert seen == []

    async def test_a_sweep_is_bounded_so_a_first_pass_cannot_stampede(self, tmp_path: Path):
        store = await _store(tmp_path)
        for _ in range(Settings().work_summary_max_per_sweep + 3):
            _ = await _seeded(store, messages=2, age_minutes=60)
        model, _ = _replies("an account")
        assert await _service(store, model).sweep() == Settings().work_summary_max_per_sweep

    async def test_a_model_failure_on_one_thread_leaves_the_others_summarized(self, tmp_path: Path):
        """One bad thread must not end the pass — the sweep is the only thing that will ever
        try again, and it only tries once a minute."""
        store = await _store(tmp_path)
        _ = await _seeded(store, messages=4, age_minutes=60)
        _ = await _seeded(store, messages=4, age_minutes=60)
        calls = {"n": 0}

        async def respond(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("the endpoint is down")
            return ModelResponse(parts=[TextPart(content="the second one")])

        assert await _service(store, FunctionModel(respond)).sweep() == 1

    async def test_a_run_anywhere_holds_the_whole_pass(self, tmp_path: Path):
        """A summary is a model call on the servers the operator chats on, so a turn in any
        other thread puts every summary off until nothing is running."""
        store = await _store(tmp_path)
        _ = await _seeded(store, messages=4, age_minutes=60)
        busy_thread = await store.create_conversation(OWNER)
        runs = RunRegistry()
        started, release = asyncio.Event(), asyncio.Event()

        async def orch(_run: Run) -> None:
            started.set()
            _ = await release.wait()

        run = runs.submit(
            kind="chat", owner_id=OWNER, orchestrator=orch, conversation_id=busy_thread
        )
        _ = await started.wait()
        model, seen = _replies("never called")
        assert await _service(store, model, runs).sweep() == 0
        assert seen == []
        release.set()
        _ = await run.wait()

    async def test_a_failing_thread_is_not_retried_every_pass(self, tmp_path: Path):
        store = await _store(tmp_path)
        _ = await _seeded(store, messages=4, age_minutes=60)
        calls = {"n": 0}

        async def respond(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
            calls["n"] += 1
            raise RuntimeError("the endpoint is down")

        service = _service(store, FunctionModel(respond))
        assert await service.sweep() == 0
        assert await service.sweep() == 0
        assert calls["n"] == 1

    async def test_a_summary_in_flight_yields_to_a_run_that_starts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The chat must never share a model with a summary: one already running is
        cancelled, and the thread is simply a candidate again once the run is over."""
        monkeypatch.setattr(work_summaries_module, "_YIELD_POLL_S", 0.01)
        store = await _store(tmp_path)
        conversation_id = await _seeded(store, messages=4, age_minutes=60)
        other = await store.create_conversation(OWNER)
        runs = RunRegistry()
        asked, release = asyncio.Event(), asyncio.Event()
        replies = {"n": 0}

        async def respond(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
            replies["n"] += 1
            if replies["n"] == 1:
                asked.set()
                _ = await asyncio.Event().wait()
            return ModelResponse(parts=[TextPart(content="after the turn")])

        async def orch(_run: Run) -> None:
            _ = await release.wait()

        service = _service(store, FunctionModel(respond), runs)
        sweep = asyncio.create_task(service.sweep())
        _ = await asyncio.wait_for(asked.wait(), 5)
        run = runs.submit(kind="chat", owner_id=OWNER, orchestrator=orch, conversation_id=other)
        assert await asyncio.wait_for(sweep, 5) == 0
        release.set()
        _ = await run.wait()

        assert await service.sweep() == 1
        summary = await store.get_summary(conversation_id, OWNER)
        assert summary is not None and summary.work_summary == "after the turn"


# --- the operator setting ---------------------------------------------------


class TestTheIdleSetting:
    def test_the_config_default(self):
        assert Settings().work_summary_idle_minutes == 15

    async def test_it_falls_back_to_the_config_default_when_unset(self):
        engine = make_engine("sqlite:///:memory:")
        init_db(engine)
        store = SettingsStore(engine)
        assert await get_work_summary_idle_minutes(store, OWNER) == (
            get_settings().work_summary_idle_minutes
        )

    async def test_it_round_trips_through_the_store(self):
        engine = make_engine("sqlite:///:memory:")
        init_db(engine)
        store = SettingsStore(engine)
        assert await set_work_summary_idle_minutes(store, OWNER, 45) == 45
        assert await get_work_summary_idle_minutes(store, OWNER) == 45

    @pytest.mark.parametrize(
        "bad", ["not-a-number", "0", "-5", str(WORK_SUMMARY_IDLE_MINUTES_MAX + 1)]
    )
    async def test_a_corrupt_or_out_of_range_value_falls_back(self, bad: str):
        """0 is in the list on purpose: it is not a faster setting but a broken one, since
        every thread would qualify the instant its turn finished."""
        engine = make_engine("sqlite:///:memory:")
        init_db(engine)
        store = SettingsStore(engine)
        await store.set(OWNER, WORK_SUMMARY_IDLE_MINUTES_KEY, bad)
        assert await get_work_summary_idle_minutes(store, OWNER) == (
            get_settings().work_summary_idle_minutes
        )

    async def test_the_sweep_reads_the_stored_setting_not_the_config_default(self, tmp_path: Path):
        """The dial is only a dial if the sweep reads it — a thread idle for 20 minutes is a
        candidate at the default 15 and not at a stored 60."""
        store = await _store(tmp_path)
        _ = await _seeded(store, messages=4, age_minutes=20)
        model, _ = _replies("an account")
        settings_engine = make_engine("sqlite:///:memory:")
        init_db(settings_engine)
        settings_store = SettingsStore(settings_engine)
        service = _service(store, model, settings_store=settings_store)
        _ = await set_work_summary_idle_minutes(settings_store, OWNER, 60)
        assert await service.sweep() == 0
        _ = await set_work_summary_idle_minutes(settings_store, OWNER, 10)
        assert await service.sweep() == 1


class TestTheChatSettingsRoute:
    async def test_it_is_exposed_and_round_trips(self):
        async with client_app() as (client, _app):
            got = json_body(await client.get("/chat/settings"))
            assert got["work_summary_idle_minutes"] == get_settings().work_summary_idle_minutes

            put = await client.put("/chat/settings", json={"work_summary_idle_minutes": 45})
            assert put.status_code == 200
            assert json_body(put)["work_summary_idle_minutes"] == 45
            reread = json_body(await client.get("/chat/settings"))
            assert reread["work_summary_idle_minutes"] == 45

    async def test_a_put_touching_only_this_leaves_the_rest_alone(self):
        async with client_app() as (client, _app):
            _ = await client.put("/chat/settings", json={"auto_compact_threshold": 0.6})
            body = json_body(
                await client.put("/chat/settings", json={"work_summary_idle_minutes": 30})
            )
            assert body["work_summary_idle_minutes"] == 30
            assert n(body["auto_compact_threshold"]) == pytest.approx(0.6)

    async def test_a_put_touching_another_field_leaves_this_alone(self):
        async with client_app() as (client, _app):
            _ = await client.put("/chat/settings", json={"work_summary_idle_minutes": 30})
            body = json_body(
                await client.put("/chat/settings", json={"auto_compact_threshold": 0.5})
            )
            assert body["work_summary_idle_minutes"] == 30

    @pytest.mark.parametrize("bad", [0, -1, WORK_SUMMARY_IDLE_MINUTES_MAX + 1, 1.5])
    async def test_an_out_of_range_window_is_rejected(self, bad: float):
        async with client_app() as (client, _app):
            resp = await client.put("/chat/settings", json={"work_summary_idle_minutes": bad})
            assert resp.status_code == 422


# --- the read path ----------------------------------------------------------


class TestTheReadPath:
    async def test_the_listing_carries_the_summary_decrypted(self, tmp_path: Path):
        """It rides the listing beside `preview` because that is the payload the re-entry
        band is drawn over — and it is sealed at rest, so the listing is also where the
        decrypt has to work."""
        store = await _store(tmp_path)
        conversation_id = await _seeded(store, messages=4, age_minutes=60)
        await store.set_work_summary(conversation_id, "Rewrote the parser; the suite is green.")
        [row] = [c for c in await store.list_conversations(OWNER) if c.id == conversation_id]
        assert row.work_summary == "Rewrote the parser; the suite is green."

    async def test_an_unsummarized_thread_reads_as_none(self, tmp_path: Path):
        store = await _store(tmp_path)
        conversation_id = await _seeded(store, messages=4, age_minutes=60)
        summary = await store.get_summary(conversation_id, OWNER)
        assert summary is not None and summary.work_summary is None

    async def test_a_second_summary_replaces_the_first(self, tmp_path: Path):
        """A plain replace, unlike the title's fill-only guard: there is no operator-authored
        twin here to clobber, and a fresher account is strictly better."""
        store = await _store(tmp_path)
        conversation_id = await _seeded(store, messages=4, age_minutes=60)
        await store.set_work_summary(conversation_id, "first")
        await store.set_work_summary(conversation_id, "second")
        summary = await store.get_summary(conversation_id, OWNER)
        assert summary is not None and summary.work_summary == "second"

    async def test_storing_a_summary_does_not_make_the_thread_look_busy(self, tmp_path: Path):
        """``updated_at`` is what both the idle window and the staleness rule read, so a
        write that bumped it would rewrite the two facts it is measured against."""
        store = await _store(tmp_path)
        conversation_id = await _seeded(store, messages=4, age_minutes=60)
        summary = await store.get_summary(conversation_id, OWNER)
        assert summary is not None
        before = summary.updated_at
        await store.set_work_summary(conversation_id, "an account")
        after = await store.get_summary(conversation_id, OWNER)
        assert after is not None
        assert after.updated_at == before
