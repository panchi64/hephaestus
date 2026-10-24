"""The stub, driven the way the app drives it.

Every test here goes through the real ``openai`` client, and the last two through the
real Pydantic AI model the registry builds — over an ASGI transport, so there is no port
and no process, but every byte of encoding and decoding is the production path. A stub
asserted against by reading its own output would only prove it is self-consistent; what
matters is that the client the app actually uses understands it.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping

import httpx
import httpx2
import pytest
from openai import AsyncOpenAI, InternalServerError
from openai.types.chat import (
    ChatCompletionFunctionToolParam,
    ChatCompletionMessageFunctionToolCall,
    ChatCompletionMessageParam,
)
from pydantic_ai import Agent
from pydantic_ai.messages import ThinkingPart
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from core.config import Settings
from devkit.stub import MODEL_ID, create_stub
from devkit.stub.embeddings import DIMENSIONS, embed
from devkit.stub.scripts import REQUEST_LOG_LIMIT
from services.endpoint_spec import EndpointSpec
from services.permissions import ReviewRequest, ScoredVerdict, capability_of
from services.permissions.decision_reviewer import make_decision_reviewer, probe

from ._json import arr, obj, parse_json

type _Stub = tuple[AsyncOpenAI, httpx2.AsyncClient]


@pytest.fixture
def stub() -> Iterator[_Stub]:
    app = create_stub()
    transport = httpx2.ASGITransport(app=app)
    http = httpx2.AsyncClient(transport=transport, base_url="http://stub")
    yield AsyncOpenAI(base_url="http://stub/v1", api_key="unused", http_client=http), http


#: Offered on every request that expects a tool call back. A scripted tool call is only
#: delivered to a request that offered tools — see the guard test below — and passing
#: them is what the agent does anyway, so the tests that want one say so.
def _offer(*names: str) -> list[ChatCompletionFunctionToolParam]:
    return [
        {"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}
        for name in names
    ]


async def _script(http: httpx2.AsyncClient, *scenarios: Mapping[str, object]) -> None:
    response = await http.post("/_stub/script", json=list(scenarios))
    assert response.status_code == 200


async def test_it_advertises_a_model_so_the_picker_can_discover_one(stub: _Stub):
    client, _ = stub
    listed = await client.models.list()
    assert [model.id for model in listed.data] == [MODEL_ID]


async def test_an_unscripted_request_echoes_rather_than_saying_something_generic(stub: _Stub):
    client, _ = stub
    answer = await client.chat.completions.create(
        model=MODEL_ID, messages=[{"role": "user", "content": "hello there"}]
    )
    assert "hello there" in (answer.choices[0].message.content or "")
    assert answer.choices[0].finish_reason == "stop"


async def test_a_scripted_reply_is_matched_on_the_last_user_message(stub: _Stub):
    client, http = stub
    await _script(http, {"match": "weather", "text": "It is raining."})
    answer = await client.chat.completions.create(
        model=MODEL_ID,
        messages=[
            {"role": "user", "content": "unrelated opener"},
            {"role": "assistant", "content": "sure"},
            {"role": "user", "content": "what is the WEATHER"},
        ],
    )
    assert answer.choices[0].message.content == "It is raining."


async def test_streaming_deltas_reassemble_into_the_same_answer(stub: _Stub):
    client, http = stub
    await _script(http, {"match": "poem", "text": "one two three four five six seven"})
    stream = await client.chat.completions.create(
        model=MODEL_ID,
        messages=[{"role": "user", "content": "a poem"}],
        stream=True,
        stream_options={"include_usage": True},
    )
    text, usage, chunks = "", None, 0
    async for chunk in stream:
        chunks += 1
        if chunk.usage is not None:
            usage = chunk.usage
        if chunk.choices and chunk.choices[0].delta.content:
            text += chunk.choices[0].delta.content
    assert text == "one two three four five six seven"
    assert chunks > 2, "the answer arrived in one delta — nothing about streaming is tested"
    # The usage frame carries no choices. A client indexing choices[0] unconditionally
    # would crash on it, so a stub that never sent one would hide a real incompatibility.
    assert usage is not None and usage.total_tokens > 0


async def test_a_scripted_tool_call_arrives_as_a_real_tool_call(stub: _Stub):
    client, http = stub
    await _script(
        http,
        {
            "match": "add",
            "tool_calls": [{"name": "calculator", "arguments": '{"a": 2, "b": 40}'}],
        },
    )
    answer = await client.chat.completions.create(
        model=MODEL_ID,
        messages=[{"role": "user", "content": "add two numbers"}],
        tools=_offer("calculator"),
    )
    calls = answer.choices[0].message.tool_calls
    assert answer.choices[0].finish_reason == "tool_calls"
    assert calls is not None
    call = calls[0]
    assert isinstance(call, ChatCompletionMessageFunctionToolCall)
    assert call.function.name == "calculator"
    assert call.function.arguments == '{"a": 2, "b": 40}'


async def test_tool_call_arguments_survive_being_split_across_deltas(stub: _Stub):
    # The fragmenting is deliberate: a stub sending whole arguments in one delta would
    # never exercise the reassembly a real server forces on the client.
    client, http = stub
    arguments = '{"query": "something long enough to be split into several fragments"}'
    scripted = {"name": "web", "arguments": arguments}
    await _script(http, {"match": "search", "tool_calls": [scripted]})
    stream = await client.chat.completions.create(
        model=MODEL_ID,
        messages=[{"role": "user", "content": "search for it"}],
        tools=_offer("web"),
        stream=True,
    )
    fragments, name = "", ""
    async for chunk in stream:
        deltas = chunk.choices[0].delta.tool_calls if chunk.choices else None
        for call in deltas or []:
            name = name or (call.function.name if call.function else "") or ""
            fragments += (call.function.arguments if call.function else "") or ""
    assert name == "web"
    assert fragments == arguments


async def test_a_tool_call_is_never_handed_to_a_request_that_offered_no_tools(stub: _Stub):
    # The failure this prevents: one user message produces several requests — the chat
    # turn that offers tools, and the auto-title beside it that offers none and carries
    # the same last user message. Without the guard the titler consumes the scripted
    # tool call, and the turn it was written for answers in plain text, which reads as
    # the agent deciding not to call the tool.
    client, http = stub
    await _script(
        http,
        {"match": "clock", "tool_calls": [{"name": "builtin_now", "arguments": "{}"}], "uses": 1},
        {"match": "clock", "text": "It is late."},
    )
    messages: list[ChatCompletionMessageParam] = [{"role": "user", "content": "check the clock"}]

    titler = await client.chat.completions.create(model=MODEL_ID, messages=messages)
    assert titler.choices[0].message.tool_calls is None

    turn = await client.chat.completions.create(
        model=MODEL_ID,
        messages=messages,
        tools=_offer("builtin_now"),
    )
    assert turn.choices[0].message.tool_calls is not None


async def test_a_scripted_failure_reaches_the_client_as_an_error(stub: _Stub):
    # It survives the client's own retries, which is why failures are unlimited by
    # default: a one-shot 500 is consumed by the retry and the caller sees success —
    # a scripted failure that silently does not fail.
    client, http = stub
    await _script(http, {"match": "boom", "status": 500, "error": "upstream exploded"})
    with pytest.raises(InternalServerError):
        _ = await client.chat.completions.create(
            model=MODEL_ID, messages=[{"role": "user", "content": "boom"}]
        )


async def test_a_counted_scenario_is_consumed_so_a_two_step_exchange_can_be_scripted(stub: _Stub):
    client, http = stub
    await _script(
        http,
        {"match": "task", "tool_calls": [{"name": "step_one", "arguments": "{}"}], "uses": 1},
        {"match": "task", "text": "Done."},
    )
    messages: list[ChatCompletionMessageParam] = [{"role": "user", "content": "do the task"}]
    tools = _offer("step_one")
    first = await client.chat.completions.create(model=MODEL_ID, messages=messages, tools=tools)
    assert first.choices[0].message.tool_calls is not None
    second = await client.chat.completions.create(model=MODEL_ID, messages=messages, tools=tools)
    assert second.choices[0].message.content == "Done."


async def test_two_scripted_tool_calls_keep_the_ids_they_were_given(stub: _Stub):
    # Ids are how a client matches each result back to the call that asked for it, so a
    # test may well assert on ones it set deliberately — being one of several must not
    # rewrite them. Only a genuine duplicate is disambiguated.
    client, http = stub
    await _script(
        http,
        {
            "match": "both",
            "tool_calls": [
                {"name": "first", "arguments": "{}", "id": "call_a"},
                {"name": "second", "arguments": "{}", "id": "call_b"},
            ],
        },
    )
    answer = await client.chat.completions.create(
        model=MODEL_ID,
        messages=[{"role": "user", "content": "do both"}],
        tools=_offer("first", "second"),
    )
    calls = answer.choices[0].message.tool_calls
    assert calls is not None
    assert [call.id for call in calls] == ["call_a", "call_b"]


async def test_duplicate_tool_call_ids_are_made_distinct(stub: _Stub):
    client, http = stub
    await _script(
        http,
        {
            "match": "twice",
            "tool_calls": [
                {"name": "same", "arguments": "{}", "id": "call_x"},
                {"name": "same", "arguments": "{}", "id": "call_x"},
            ],
        },
    )
    answer = await client.chat.completions.create(
        model=MODEL_ID,
        messages=[{"role": "user", "content": "do it twice"}],
        tools=_offer("same"),
    )
    calls = answer.choices[0].message.tool_calls
    assert calls is not None
    assert len({call.id for call in calls}) == 2


async def test_the_request_log_is_bounded(stub: _Stub):
    # The stub outlives any one test. A request carries the whole replayed transcript
    # plus every tool schema, so an unbounded log grows without limit and eventually
    # makes `/_stub/requests` too large to read.
    client, http = stub
    for index in range(REQUEST_LOG_LIMIT + 5):
        _ = await client.chat.completions.create(
            model=MODEL_ID, messages=[{"role": "user", "content": f"message {index}"}]
        )
    recorded = obj(parse_json((await http.get("/_stub/requests")).content))
    assert recorded["count"] == REQUEST_LOG_LIMIT
    # Oldest first, and it is the newest that are kept.
    requests = arr(recorded["requests"])
    last = arr(obj(requests[-1])["messages"])
    assert obj(last[-1])["content"] == f"message {REQUEST_LOG_LIMIT + 4}"


async def test_it_records_what_the_agent_actually_sent(stub: _Stub):
    client, http = stub
    _ = await client.chat.completions.create(
        model=MODEL_ID,
        messages=[{"role": "user", "content": "hi"}],
        tools=_offer("calculator"),
    )
    recorded = obj(parse_json((await http.get("/_stub/requests")).content))
    assert recorded["count"] == 1
    tool = obj(arr(obj(arr(recorded["requests"])[0])["tools"])[0])
    assert obj(tool["function"])["name"] == "calculator"


async def test_embeddings_are_stable_and_unrelated_texts_are_not_close(stub: _Stub):
    client, _ = stub
    response = await client.embeddings.create(model=MODEL_ID, input=["alpha", "beta"])
    alpha, beta = (item.embedding for item in response.data)
    assert len(alpha) == DIMENSIONS
    assert alpha == embed("alpha")  # same text, same vector, run after run
    assert abs(sum(a * b for a, b in zip(alpha, beta, strict=True))) < 0.3


async def test_the_pydantic_ai_model_the_registry_builds_can_talk_to_it(stub: _Stub):
    # The end of the chain: not the OpenAI client but the model object the app's own
    # `openai-compatible` adapter constructs, running a real agent turn against it.
    _client, http = stub
    await _script(http, {"match": "ping", "text": "pong"})
    model = OpenAIChatModel(
        MODEL_ID,
        provider=OpenAIProvider(base_url="http://stub/v1", api_key="unused", http_client=http),
    )
    result = await Agent(model).run("ping")
    assert result.output == "pong"


async def test_scripted_reasoning_streams_as_pydantic_ai_thinking(stub: _Stub):
    _client, http = stub
    await _script(http, {"match": "ponder", "reasoning": "weighing it up first", "text": "done"})
    model = OpenAIChatModel(
        MODEL_ID,
        provider=OpenAIProvider(base_url="http://stub/v1", api_key="unused", http_client=http),
    )
    async with Agent(model).run_stream("ponder") as run:
        output = await run.get_output()
        response = run.all_messages()[-1]
    thinking = [part for part in response.parts if isinstance(part, ThinkingPart)]
    assert output == "done"
    assert "".join(part.content for part in thinking) == "weighing it up first"


async def _decide_through_the_stub(*overrides: Mapping[str, object]) -> object:
    """One Auto review by the app's own decision reviewer, answered by the stub."""
    app = create_stub()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
        if overrides:
            pushed = await client.post("http://stub/_stub/decisions", json=list(overrides))
            assert pushed.status_code == 200
        reviewer = make_decision_reviewer(
            EndpointSpec(base_url="http://stub/v1", model=MODEL_ID),
            settings=Settings(),
            client=client,
        )
        capability = capability_of("mail_send", {"to": "a@b.c"})
        return await reviewer(ReviewRequest(capability=capability))


async def test_the_stub_clears_an_action_by_default():
    result = await _decide_through_the_stub()
    assert isinstance(result, ScoredVerdict)
    assert (result.verdict.risk, result.verdict.authorization) == ("low", "explicitly_yes")


async def test_the_bind_probe_leaves_a_pushed_override_for_the_review():
    app = create_stub()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
        _ = await client.post("http://stub/_stub/decisions", json=[{"refused": {"noul": 0.9}}])
        await probe(
            EndpointSpec(base_url="http://stub/v1", model=MODEL_ID),
            settings=Settings(),
            client=client,
        )
        pending = await client.post("http://stub/_stub/decisions", json=[])
    assert parse_json(pending.content) == {"pending": 1}


async def test_a_pushed_decision_overrides_the_named_answers_once():
    result = await _decide_through_the_stub({"refused": {"noul": 0.9}})
    assert isinstance(result, ScoredVerdict)
    assert result.verdict.authorization == "explicitly_no"
