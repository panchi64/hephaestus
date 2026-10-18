"""The decision client: where it posts, what it sends, and every way an answer is refused."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from services import decisions
from services.decisions import (
    Choice,
    ChoiceAnswer,
    DecisionUnavailable,
    Noul,
    NoulAnswer,
    Question,
    Score,
    ScoreAnswer,
)
from services.endpoint_spec import EndpointSpec

from ._json import JSON, obj

QUESTIONS: dict[str, Question] = {
    "yes": Noul("Is it?"),
    "team": Choice("Which team?", {"billing": "Money", "tech": "Faults"}),
    "level": Score("How bad?", ["Fine", "Bad", "Awful"]),
}

GOOD_ANSWERS: dict[str, object] = {
    "yes": {"noul": 0.9},
    "team": {"choice": "tech", "probabilities": {"billing": 0.2, "tech": 0.8}},
    "level": {"score": 1.2, "probabilities": [0.1, 0.6, 0.3], "confidence": 0.4},
}
GOOD = {"answers": GOOD_ANSWERS}


def _client(
    reply: Callable[[httpx.Request], httpx.Response], seen: list[httpx.Request]
) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return reply(request)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _json(body: object, status: int = 200) -> Callable[[httpx.Request], httpx.Response]:
    def reply(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body)

    return reply


@pytest.mark.parametrize(
    ("base_url", "url"),
    [
        ("http://127.0.0.1:8080/v1", "http://127.0.0.1:8080/v1/systemone"),
        ("http://localhost:8080", "http://localhost:8080/v1/systemone"),
        ("https://gpu.lan/llama/v1/", "https://gpu.lan/llama/v1/systemone"),
        ("https://openrouter.ai/api/v1", "https://openrouter.ai/api/alpha/decisions"),
    ],
)
def test_the_url_depends_on_the_host_and_not_the_path(base_url: str, url: str):
    assert decisions.decisions_url(base_url) == url


async def test_it_sends_the_model_the_state_the_questions_and_the_key():
    seen: list[httpx.Request] = []
    spec = EndpointSpec(
        base_url="https://openrouter.ai/api/v1", model="typesafe/jev-1.13", api_key="k"
    )
    async with _client(_json(GOOD), seen) as client:
        answers = await decisions.ask(spec, {"x": 1}, QUESTIONS, timeout_s=5, client=client)

    body = obj(JSON.validate_json(seen[0].content))
    questions = obj(body["questions"])
    assert body["model"] == "typesafe/jev-1.13"
    assert body["state"] == {"x": 1}
    assert questions["yes"] == {"type": "noul", "instructions": "Is it?"}
    assert obj(questions["team"])["criteria"] == {"billing": "Money", "tech": "Faults"}
    assert obj(questions["level"])["criteria"] == ["Fine", "Bad", "Awful"]
    assert seen[0].headers["Authorization"] == "Bearer k"
    assert answers["yes"] == NoulAnswer(noul=0.9)
    assert isinstance(answers["team"], ChoiceAnswer)
    assert isinstance(answers["level"], ScoreAnswer)


async def test_a_keyless_local_server_gets_no_auth_header():
    seen: list[httpx.Request] = []
    async with _client(_json(GOOD), seen) as client:
        _ = await decisions.ask(
            EndpointSpec(base_url="http://127.0.0.1:8080/v1", model="kev"),
            None,
            QUESTIONS,
            timeout_s=5,
            client=client,
        )
    assert "Authorization" not in seen[0].headers


LOCAL = EndpointSpec(base_url="http://127.0.0.1:8080/v1", model="kev")


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"answers": {**GOOD_ANSWERS, "yes": {"noul": 1.4}}}, id="out-of-range"),
        pytest.param(
            {"answers": {k: v for k, v in GOOD_ANSWERS.items() if k != "team"}}, id="missing"
        ),
        pytest.param({"answers": {**GOOD_ANSWERS, "yes": {"score": 0.5}}}, id="wrong-type"),
        pytest.param(
            {"answers": {**GOOD_ANSWERS, "team": {"choice": "sales", "probabilities": {}}}},
            id="unknown-option",
        ),
        pytest.param(
            {"answers": {**GOOD_ANSWERS, "level": {"score": 1.0, "probabilities": [1.0]}}},
            id="wrong-level-count",
        ),
        pytest.param({"choices": []}, id="chat-shaped"),
    ],
)
async def test_an_unusable_answer_is_refused(body: object):
    async with _client(_json(body), []) as client:
        with pytest.raises(DecisionUnavailable):
            _ = await decisions.ask(LOCAL, None, QUESTIONS, timeout_s=5, client=client)


@pytest.mark.parametrize("status", [404, 500, 501])
async def test_a_failing_status_is_refused(status: int):
    async with _client(_json({}, status), []) as client:
        with pytest.raises(DecisionUnavailable, match=str(status)):
            _ = await decisions.ask(LOCAL, None, QUESTIONS, timeout_s=5, client=client)


async def test_an_unreachable_server_is_refused():
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    async with _client(refuse, []) as client:
        with pytest.raises(DecisionUnavailable, match="couldn't reach"):
            _ = await decisions.ask(LOCAL, None, QUESTIONS, timeout_s=5, client=client)


async def test_the_probe_asks_one_question():
    seen: list[httpx.Request] = []
    async with _client(_json({"answers": {"probe": {"noul": 0.99}}}), seen) as client:
        await decisions.probe(LOCAL, timeout_s=5, client=client)
    assert list(obj(obj(JSON.validate_json(seen[0].content))["questions"])) == ["probe"]
