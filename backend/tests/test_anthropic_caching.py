"""Anthropic request shape: cache TTL by who waits on the run, pinned effort, diagnostics,
and an instruction head that a changing block doesn't rewrite."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx2
import pytest
from pydantic import JsonValue
from pydantic_ai import (
    Agent,
    AgentInstructionSource,
    InstructionBaselinePart,
    InstructionDeltaPart,
    InstructionId,
    InstructionPart,
    ModelRequest,
    ModelResponse,
    ModelSettingsDeltaPart,
    RequestUsage,
    RunContext,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.messages import CompactionPart, InstructionDigest, ModelMessage
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.profiles import ModelProfile
from pydantic_ai.providers.anthropic import AnthropicProvider as SdkAnthropicProvider
from pydantic_ai.settings import ModelSettings, ThinkingEffort

import services.providers.anthropic as anthropic_provider
from agent import build_chat_orchestrator, stream_agent_run
from agent.factory import build_agent
from agent.prefix_watch import digest_request
from core.container import ServiceContainer
from core.db import init_db, make_engine
from core.vault import Vault
from routes.deps import app_state
from runs import Run, RunRegistry, RunStream
from services import llm
from services.conversations import ConversationStore, conversation_totals, last_request_usage
from services.conversations.usage import response_cost
from services.endpoint_spec import EndpointSpec
from services.providers import get_provider, one_shot_settings, run_settings
from services.reasoning import parse_effort
from services.registry import ModelRegistry
from tools import RunDeps
from tools.deps import PromptContextRequest

from ._helpers import client_app, register_stub_provider, settle, stub_resolution
from ._json import json_body, s

CACHE_KEYS = ("anthropic_cache", "anthropic_cache_instructions", "anthropic_cache_tool_definitions")
OWNER = "op"


def _ttl(value: str) -> Callable[[], SimpleNamespace]:
    return lambda: SimpleNamespace(anthropic_cache_ttl=value)


def _spec(
    *, base_url: str = "https://api.anthropic.com", effort: str | None = None
) -> EndpointSpec:
    return EndpointSpec(
        base_url=base_url,
        model="claude-opus-5-5",
        provider="anthropic",
        api_key="k",
        effort=parse_effort(effort),
    )


class TestTtlByRunKind:
    @pytest.mark.parametrize(
        ("configured", "attended", "expected"),
        [
            ("auto", True, "1h"),
            ("auto", False, "5m"),
            ("5m", True, "5m"),
            ("1h", False, "1h"),
        ],
    )
    def test_resolves_per_run(
        self, monkeypatch: pytest.MonkeyPatch, configured: str, attended: bool, expected: str
    ):
        monkeypatch.setattr(anthropic_provider, "get_settings", _ttl(configured))
        settings = get_provider("anthropic").run_settings(attended=attended)
        assert {key: settings.get(key) for key in CACHE_KEYS} == dict.fromkeys(CACHE_KEYS, expected)

    def test_off_sends_nothing(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(anthropic_provider, "get_settings", _ttl("off"))
        assert get_provider("anthropic").run_settings(attended=True) == {}

    def test_the_merged_run_settings_carry_anthropics_keys(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(anthropic_provider, "get_settings", _ttl("auto"))
        assert run_settings(attended=True).get("anthropic_cache") == "1h"

    @pytest.mark.parametrize(("kind", "expected"), [("chat", "1h"), ("linked", "5m")])
    async def test_a_turn_carries_the_ttl_for_its_kind(
        self, monkeypatch: pytest.MonkeyPatch, kind: str, expected: str
    ):
        monkeypatch.setattr(anthropic_provider, "get_settings", _ttl("auto"))
        seen: list[AgentInfo] = []
        await _drive(_answers(seen), _deps(kind))
        settings = seen[0].model_settings or {}
        assert settings.get("anthropic_cache") == expected
        assert settings.get("parallel_tool_calls") is True


_MESSAGE_STREAM: list[dict[str, JsonValue]] = [
    {
        "type": "message_start",
        "message": {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-5-5",
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 0},
        },
    },
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "ok"}},
    {"type": "content_block_stop", "index": 0},
    {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": {"output_tokens": 1},
    },
    {"type": "message_stop"},
]


def _sent_bodies(bodies: list[str]) -> httpx2.AsyncClient:
    def reply(request: httpx2.Request) -> httpx2.Response:
        bodies.append(request.content.decode())
        events = "".join(
            f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in _MESSAGE_STREAM
        )
        return httpx2.Response(
            200, headers={"content-type": "text/event-stream"}, content=events.encode()
        )

    return httpx2.AsyncClient(transport=httpx2.MockTransport(reply))


class TestOneShotCallsSkipTheCache:
    async def _send(self, run: ModelSettings | None) -> str:
        bodies: list[str] = []
        built = llm.build_model(_spec())
        model = AnthropicModel(
            "claude-opus-5-5",
            provider=SdkAnthropicProvider(api_key="k", http_client=_sent_bodies(bodies)),
            settings=built.settings,
        )
        _ = await Agent(model, instructions="Name the thread.").run("hello", model_settings=run)
        return bodies[0]

    async def test_a_one_shot_request_sends_no_breakpoint(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(anthropic_provider, "get_settings", _ttl("1h"))
        assert "cache_control" in await self._send(None)
        assert "cache_control" not in await self._send(one_shot_settings())


class TestEffortAndDiagnostics:
    def test_effort_defaults_to_high(self):
        assert (llm.build_model(_spec()).settings or {}).get("thinking") == "high"

    def test_the_endpoints_effort_is_sent(self):
        assert (llm.build_model(_spec(effort="xhigh")).settings or {}).get("thinking") == "xhigh"

    def test_an_unrecognized_stored_effort_falls_back_to_the_default(self):
        assert (llm.build_model(_spec(effort="extreme")).settings or {}).get("thinking") == "high"

    def test_diagnostics_only_on_the_first_party_api(self):
        first_party = llm.build_model(_spec()).settings or {}
        proxy = llm.build_model(_spec(base_url="https://proxy.example/v1")).settings or {}
        assert first_party.get("anthropic_cache_diagnostics") is True
        assert "anthropic_cache_diagnostics" not in proxy


class TestAppendModeHead:
    async def test_a_changed_block_arrives_as_a_delta_not_a_new_head(self):
        """The head a conversation opened with is what every later request is sent."""
        catalog = {"text": "memories: a"}

        def memory_catalog_instructions(_ctx: RunContext[RunDeps]) -> str:
            return catalog["text"]

        seen: list[AgentInfo] = []
        model = _answers(seen)
        history: list[ModelMessage] = []
        for text in ("memories: a", "memories: a, b"):
            catalog["text"] = text
            agent = build_agent(model, instruction_providers=(memory_catalog_instructions,))
            deps = _deps("chat")
            async with agent.iter("go", deps=deps, message_history=history) as agent_run:
                await stream_agent_run(agent_run, deps.run)
            assert agent_run.result is not None
            history = agent_run.result.all_messages()

        assert seen[0].instructions == seen[1].instructions
        assert seen[1].instructions is not None and "memories: a, b" not in seen[1].instructions
        deltas = [
            part
            for message in history
            if isinstance(message, ModelRequest)
            for part in message.parts
            if isinstance(part, InstructionDeltaPart)
        ]
        assert [delta.content for delta in deltas] == ["memories: a, b"]


class TestTheWatchReadsTheHeadTheLibrarySends:
    """The prefix watch's reading follows the library's instruction window."""

    _MEMORY: InstructionPart = InstructionPart(
        content="memories: a",
        id=InstructionId(AgentInstructionSource(), name="memory"),
        on_change="append",
    )
    _RULES: InstructionPart = InstructionPart(content="rules v1")

    def _history(self) -> list[ModelMessage]:
        baseline = InstructionBaselinePart(
            instructions=[self._MEMORY, InstructionDigest.of(self._RULES)]
        )
        return [
            ModelRequest(parts=[UserPromptPart("go"), baseline]),
            ModelResponse(parts=[TextPart("ok")]),
        ]

    def _head(self, parts: list[InstructionPart], messages: list[ModelMessage]) -> str:
        return digest_request(parts, [], messages).instructions

    def test_a_changed_append_block_keeps_the_windows_head(self):
        changed = replace(self._MEMORY, content="memories: a, b")
        assert self._head([changed, self._RULES], self._history()) == self._head(
            [self._MEMORY, self._RULES], []
        )

    def test_a_changed_rewrite_block_opens_a_new_window(self):
        current = [replace(self._MEMORY, content="memories: a, b"), InstructionPart("rules v2")]
        assert self._head(current, self._history()) == self._head(current, [])

    def test_a_compaction_ends_the_window(self):
        current = [replace(self._MEMORY, content="memories: a, b"), self._RULES]
        folded = [*self._history(), ModelResponse(parts=[CompactionPart(content="summary")])]
        assert self._head(current, folded) == self._head(current, [])


class TestReplayMatchesWhatWasSent:
    async def test_the_next_turn_replays_the_prompt_and_head_it_was_sent(self, tmp_path: Path):
        """The tail context and the instruction baseline both survive the save, so turn 2's
        prefix is byte-for-byte what turn 1 cached."""
        engine = make_engine("sqlite:///:memory:")
        init_db(engine)
        vault = Vault(tmp_path / "keyfile.json")
        await vault.setup("pw")
        store = ConversationStore(engine, vault)
        await store.start()
        conversation_id = await store.create_conversation(OWNER)

        catalog = {"text": "memories: a"}
        sent: list[tuple[list[ModelMessage], AgentInfo]] = []

        def memory_catalog_instructions(_ctx: RunContext[RunDeps]) -> str:
            return catalog["text"]

        async def tasks_context(_request: PromptContextRequest) -> str:
            return f"<tasks>turn {len(sent) + 1}</tasks>"

        async def stream_fn(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
            sent.append((list(messages), info))
            yield "ok"

        registry = RunRegistry()
        for prompt, text in (("first", "memories: a"), ("second", "memories: a, b")):
            catalog["text"] = text
            orchestrator = build_chat_orchestrator(
                prompt,
                model=FunctionModel(stream_function=stream_fn),
                categories={},
                store=store,
                conversation_id=conversation_id,
                instruction_providers=(memory_catalog_instructions,),
                prompt_context_providers=(tasks_context,),
            )
            run = registry.submit(kind="chat", owner_id=OWNER, orchestrator=orchestrator)
            await run.wait()
        stored = await store.history(conversation_id)
        await store.stop()

        (first, first_info), (second, second_info) = sent
        assert _prompt(second[0]) == _prompt(first[-1]) == ["first", "<tasks>turn 1</tasks>"]
        assert second_info.instructions == first_info.instructions
        # The change still reaches the model, after the history rather than in the head.
        assert "memories: a, b" in str(second[-1])
        assert isinstance(stored[0], ModelRequest)
        assert any(isinstance(p, InstructionBaselinePart) for p in stored[0].parts)


def _response(
    usage: RequestUsage,
    *,
    url: str = "https://api.anthropic.com",
    details: dict[str, object] | None = None,
) -> ModelResponse:
    return ModelResponse(
        parts=[TextPart("ok")],
        usage=usage,
        model_name="claude-opus-5-5",
        provider_name="anthropic",
        provider_url=url,
        provider_details=details,
    )


class TestCostAndDiagnostics:
    def test_a_cached_request_costs_less_and_says_by_how_much(self):
        cost = response_cost(
            _response(RequestUsage(input_tokens=1_000_000, cache_read_tokens=800_000))
        )
        assert cost is not None
        # Opus 5.5: $4/MTok input, $0.20/MTok cache read.
        assert cost.total_usd == pytest.approx(0.96)
        assert cost.saved_usd == pytest.approx(3.04)

    def test_a_remembered_price_follows_a_change_to_the_usage(self):
        response = _response(RequestUsage(input_tokens=1_000_000))
        first = response_cost(response)
        assert response_cost(response) == first
        response.usage = RequestUsage(input_tokens=2_000_000)
        second = response_cost(response)
        assert first is not None and second is not None
        assert second.total_usd == pytest.approx(2 * first.total_usd)

    def test_an_unknown_endpoint_is_never_priced(self):
        """A local server serving a model under a hosted name must not be billed for it."""
        usage = RequestUsage(input_tokens=1_000, output_tokens=10)
        assert response_cost(_response(usage, url="http://localhost:8080/v1")) is None

    def test_totals_sum_writes_and_cost(self):
        usage = RequestUsage(input_tokens=10_000, cache_write_tokens=9_000, output_tokens=100)
        totals = conversation_totals([_response(usage), _response(usage)])
        assert totals.cache_write_tokens == 18_000
        assert totals.cost_usd is not None and totals.cost_usd > 0
        # Writes cost 1.25x and nothing was read back: caching cost money here.
        assert totals.saved_usd is not None and totals.saved_usd < 0

    def test_the_diagnostics_verdict_reaches_the_last_request(self):
        miss = {"cache_miss_reason": {"type": "system_changed", "cache_missed_input_tokens": 41}}
        response = _response(RequestUsage(input_tokens=100), details={"cache_diagnostics": miss})
        last = last_request_usage([response])
        assert last is not None
        assert (last.cache_miss_reason, last.cache_missed_tokens) == ("system_changed", 41)


def _recording_resolution(
    monkeypatch: pytest.MonkeyPatch, seen: list[AgentInfo], *, uses_effort: bool
) -> None:
    async def stream_fn(_messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        seen.append(info)
        yield "ok"

    def build(settings: ModelSettings | None = None) -> FunctionModel:
        # A profile that thinks, as Claude's does; the library drops `thinking` otherwise.
        return FunctionModel(
            stream_function=stream_fn,
            profile=ModelProfile(supports_thinking=True),
            settings=settings,
        )

    def with_effort(effort: ThinkingEffort) -> FunctionModel:
        return build({"thinking": effort})

    async def resolve_detailed(self: ModelRegistry, _role: str, **_kwargs: object):
        return await stub_resolution(
            self, build(), with_effort=with_effort if uses_effort else None
        )

    register_stub_provider(monkeypatch)
    monkeypatch.setattr(ModelRegistry, "resolve_detailed", resolve_detailed)


class TestThreadEffort:
    async def test_a_send_sets_the_threads_effort_and_the_turn_runs_at_it(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        seen: list[AgentInfo] = []
        _recording_resolution(monkeypatch, seen, uses_effort=True)
        async with client_app() as (client, app):
            first = json_body(await client.post("/chat", json={"prompt": "a", "effort": "max"}))
            conversation_id = s(first["conversation_id"])
            await settle(app)
            detail = json_body(await client.get(f"/conversations/{conversation_id}"))
            assert detail["effort"] == "max"

            body = {"prompt": "b", "conversation_id": conversation_id, "effort": "low"}
            _ = await client.post("/chat", json=body)
            await settle(app)
            history = await app_state(app).conversations.history(conversation_id)

            cleared = {"prompt": "c", "conversation_id": conversation_id, "effort": ""}
            _ = await client.post("/chat", json=cleared)
            await settle(app)
            detail = json_body(await client.get(f"/conversations/{conversation_id}"))

        # The library resolves unified `thinking` onto the request parameters.
        efforts = [info.model_request_parameters.thinking for info in seen]
        assert efforts == ["max", "low", None]
        assert detail["effort"] is None
        # The change rides the history, where a model that supports it sends it in place.
        assert any(
            isinstance(part, ModelSettingsDeltaPart)
            for message in history
            if isinstance(message, ModelRequest)
            for part in message.parts
        )

    async def test_an_endpoint_without_effort_never_gets_the_threads(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        seen: list[AgentInfo] = []
        _recording_resolution(monkeypatch, seen, uses_effort=False)
        async with client_app() as (client, app):
            _ = await client.post("/chat", json={"prompt": "a", "effort": "max"})
            await settle(app)
        assert seen[0].model_request_parameters.thinking is None


def _prompt(message: ModelMessage) -> object:
    assert isinstance(message, ModelRequest)
    return next(p.content for p in message.parts if isinstance(p, UserPromptPart))


def _deps(kind: str) -> RunDeps:
    run = Run(id="t", kind=kind, owner_id=OWNER, stream=RunStream())
    return RunDeps(run=run, owner_id=OWNER, caps=ServiceContainer())


def _answers(seen: list[AgentInfo]) -> FunctionModel:
    async def stream_fn(_messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        seen.append(info)
        yield "done"

    return FunctionModel(stream_function=stream_fn)


async def _drive(model: FunctionModel, deps: RunDeps) -> None:
    agent = build_agent(model)
    async with agent.iter("go", deps=deps) as agent_run:
        await stream_agent_run(agent_run, deps.run)
