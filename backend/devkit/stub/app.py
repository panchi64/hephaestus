"""An OpenAI-compatible server that says what it is told to.

The dev instance needs a model behind it, and a real one is the wrong tool for the job it
has here: it costs money or a running local engine, it answers differently every time, it
cannot be made to fail on demand, and it cannot be made to take four seconds so somebody
can look at a spinner. This answers all four, and it plugs in through the registry's
ordinary ``openai-compatible`` provider with no key — the same path a local engine takes,
so nothing about the instance is special-cased to accommodate it.

It is also a listening post. Every request body is recorded, so a test can assert what
the agent *sent* — which tools it offered, which instructions it carried, how much
transcript it replayed — and not merely what it did with the answer.

Deliberately not mounted into the app: it runs as its own process on its own port, so
editing the backend restarts the backend and leaves the pushed script standing.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncGenerator, Iterable

from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, JsonValue

from devkit.stub.embeddings import embed as embed_text
from devkit.stub.scripts import Scenario, ScriptBook, last_user_prompt
from devkit.stub.wire import Reply, ToolCall, completion, stream

#: The model id the stub advertises and the seeded endpoint binds to.
MODEL_ID = "hephaestus-stub"


class ToolCallIn(BaseModel):
    name: str
    arguments: str = "{}"
    id: str = "call_stub"


class ScenarioIn(BaseModel):
    """One scripted reply, as the control plane accepts it."""

    match: str | None = None
    text: str = ""
    tool_calls: list[ToolCallIn] = []
    needs_tools: bool | None = None
    latency_s: float = 0.0
    chunk_delay_s: float = 0.0
    status: int = 200
    error: str = "scripted failure"
    uses: int | None = None

    def to_scenario(self) -> Scenario:
        return Scenario(
            match=self.match,
            text=self.text,
            tool_calls=[ToolCall(c.name, c.arguments, c.id) for c in self.tool_calls],
            needs_tools=self.needs_tools,
            latency_s=self.latency_s,
            chunk_delay_s=self.chunk_delay_s,
            status=self.status,
            error=self.error,
            uses=self.uses,
        )


def create_stub() -> FastAPI:
    """A stub with its own script. A factory rather than a module-level app so two of
    them in one test process cannot share state."""
    app = FastAPI(title="Hephaestus stub model", docs_url=None, redoc_url=None)
    book = ScriptBook()
    app.state.book = book

    @app.get("/v1/models")
    async def list_models() -> dict[str, JsonValue]:
        # The picker discovers what an endpoint offers through this, and the registry's
        # connection test probes it — so a stub without it reads as an unreachable
        # endpoint rather than a working one.
        return {
            "object": "list",
            "data": [{"id": MODEL_ID, "object": "model", "owned_by": "devkit"}],
        }

    @app.post("/v1/chat/completions")
    async def chat_completions(body: dict[str, JsonValue]):
        book.requests.append(body)
        messages = body.get("messages")
        scenario = book.next_for(
            last_user_prompt(messages if isinstance(messages, list) else []),
            has_tools=bool(body.get("tools")),
        )
        if scenario.latency_s:
            await asyncio.sleep(scenario.latency_s)
        if scenario.status >= 400:
            return JSONResponse(
                status_code=scenario.status,
                content={"error": {"message": scenario.error, "type": "stub_error"}},
            )

        requested = body.get("model")
        model = requested if isinstance(requested, str) and requested else MODEL_ID
        request_id = f"chatcmpl-{secrets.token_hex(8)}"
        reply: Reply = scenario.reply()
        if not body.get("stream"):
            return completion(model, reply, request_id=request_id)

        options = body.get("stream_options")
        include_usage = isinstance(options, dict) and bool(options.get("include_usage"))
        return StreamingResponse(
            _aiter(
                stream(model, reply, request_id=request_id, include_usage=include_usage),
                scenario.chunk_delay_s,
            ),
            media_type="text/event-stream",
        )

    @app.post("/v1/embeddings")
    async def embed(body: dict[str, JsonValue]) -> dict[str, JsonValue]:
        text = body.get("input") or []
        texts = (
            [text]
            if isinstance(text, str)
            else [str(item) for item in text]
            if isinstance(text, list)
            else []
        )
        return {
            "object": "list",
            "model": body.get("model") or MODEL_ID,
            "data": [
                {"object": "embedding", "index": index, "embedding": list(embed_text(item))}
                for index, item in enumerate(texts)
            ],
            "usage": {"prompt_tokens": len(texts), "total_tokens": len(texts)},
        }

    decisions: list[dict[str, JsonValue]] = []

    @app.post("/v1/systemone")
    async def decide(body: dict[str, JsonValue]) -> dict[str, JsonValue]:
        # The decision wire (`services/decisions.py`). By default every action is low risk
        # and requested, so Auto clears it; a pushed override replaces named answers once.
        book.requests.append(body)
        questions = body.get("questions")
        override = decisions.pop(0) if decisions else {}
        answers: dict[str, JsonValue] = {}
        for name, question in (questions if isinstance(questions, dict) else {}).items():
            answers[name] = override.get(name) or _default_answer(name, question)
        return {"answers": answers}

    @app.post("/_stub/decisions")
    async def push_decisions(overrides: list[dict[str, JsonValue]]) -> dict[str, int]:
        decisions.extend(overrides)
        return {"pending": len(decisions)}

    @app.post("/_stub/script")
    async def push_script(scenarios: list[ScenarioIn]) -> dict[str, int]:
        book.push([item.to_scenario() for item in scenarios])
        return {"pending": book.pending}

    @app.post("/_stub/reset")
    async def reset() -> dict[str, int]:
        book.reset()
        decisions.clear()
        return {"pending": book.pending}

    @app.get("/_stub/requests")
    async def requests() -> dict[str, JsonValue]:
        return {"count": len(book.requests), "requests": [*book.requests]}

    return app


def _default_answer(name: str, question: JsonValue) -> JsonValue:
    """A confident "low risk, and the operator asked for it" for each question type."""
    kind = question.get("type") if isinstance(question, dict) else None
    criteria = question.get("criteria") if isinstance(question, dict) else None
    if kind == "score":
        levels = len(criteria) if isinstance(criteria, list) else 2
        probabilities: list[JsonValue] = [0.0] * levels
        probabilities[0] = 0.9
        if levels > 1:
            probabilities[1] = 0.1
        return {"score": 0.1, "probabilities": probabilities, "confidence": 0.8}
    if kind == "choice":
        options = list(criteria) if isinstance(criteria, dict) else []
        first = options[0] if options else ""
        return {"choice": first, "probabilities": {first: 1.0}, "confidence": 1.0}
    return {"noul": 0.95 if name == "requested" else 0.02}


async def _aiter(frames: Iterable[str], delay_s: float = 0.0) -> AsyncGenerator[str]:
    """The sync generator `wire.stream` produces, as the async one Starlette wants."""
    for frame in frames:
        yield frame
        if delay_s:
            await asyncio.sleep(delay_s)
