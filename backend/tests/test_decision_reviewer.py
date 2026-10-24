"""The bind-time probe: a full-size review, so a server that only manages short ones fails."""

from __future__ import annotations

import httpx
import pytest
from pydantic import JsonValue

from core.config import Settings
from services.decisions import DecisionUnavailable
from services.endpoint_spec import EndpointSpec
from services.permissions.decision_questions import QUESTIONS
from services.permissions.decision_reviewer import PROBE_NONCE, probe

from ._json import JSON, obj

SPEC = EndpointSpec(base_url="http://127.0.0.1:1234/v1", model="vllm-d3")
SETTINGS = Settings(
    review_transcript_entries=3, review_transcript_chars=3_000, review_ledger_entries=2
)


def _answer(question: JsonValue) -> JsonValue:
    kind = obj(question)["type"]
    if kind == "score":
        return {"score": 0.0, "probabilities": [1.0, 0.0, 0.0, 0.0]}
    return {"noul": 0.5}


async def test_the_probe_asks_every_review_question_about_a_full_size_review():
    seen: list[httpx.Request] = []

    def reply(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        questions = obj(obj(JSON.validate_json(request.content))["questions"])
        answers = {name: _answer(question) for name, question in questions.items()}
        return httpx.Response(200, json={"answers": answers})

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        await probe(SPEC, settings=SETTINGS, client=client)

    body = obj(JSON.validate_json(seen[0].content))
    assert set(obj(body["questions"])) == set(QUESTIONS)
    state = obj(body["state"])
    assert PROBE_NONCE in str(state["notice"])
    assert isinstance(state["approved_calls"], list)
    assert len(state["approved_calls"]) == SETTINGS.review_ledger_entries
    conversation = state["conversation"]
    assert isinstance(conversation, str)
    # Filled to the transcript budget, and no further.
    assert SETTINGS.review_transcript_chars < len(conversation)
    assert len(conversation) < 2 * SETTINGS.review_transcript_chars


async def test_a_server_that_refuses_the_full_size_review_fails_the_probe():
    def reply(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "input (9000 tokens) is too large"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        with pytest.raises(DecisionUnavailable, match="too large"):
            await probe(SPEC, settings=SETTINGS, client=client)
