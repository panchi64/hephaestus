"""A cold-load failure from the inference server becomes an actionable run error.

On-demand servers (LM Studio, llama.cpp, …) reject a request for a model they
can't bring up with a terse, mechanical message — most often when a side-by-side
compare fires two *unloaded* models at once and the server can only cold-load one.
The engine rewrites that into something the operator can act on; everything else
keeps its own detail.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any, override

from pydantic_ai import RunContext
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models import ModelRequestParameters, StreamedResponse
from pydantic_ai.models.test import TestModel
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ModelSettings

from agent import build_chat_orchestrator
from agent.model_errors import model_load_hint
from runs import RunRegistry, RunStatus


def _abort_error(model: str = "qwen3.6-35b-a3b-mtp") -> ModelHTTPError:
    message = f'Failed to load model "{model}". Error: Engine protocol startup was aborted.'
    return ModelHTTPError(status_code=400, model_name=model, body={"message": message})


def test_hint_detects_an_engine_load_abort():
    hint = model_load_hint(_abort_error())
    assert hint is not None
    assert "qwen3.6-35b-a3b-mtp" in hint  # names the model that wouldn't load
    assert "LM Studio" in hint  # points at the engine-side fix


def test_hint_ignores_unrelated_http_errors():
    other = ModelHTTPError(status_code=500, model_name="m", body={"message": "internal error"})
    assert model_load_hint(other) is None


class _LoadFailingModel(WrapperModel):
    """A model whose every request fails the way an inference server does when it
    can't bring the model up — so a run drives straight into the abort."""

    def __init__(self) -> None:
        super().__init__(TestModel())
        self.failure: Exception | None = _abort_error()

    @override
    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        raise _abort_error()

    @override
    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: RunContext[Any] | None = None,
    ) -> AsyncGenerator[StreamedResponse]:
        if self.failure is not None:
            raise self.failure
        async with self.wrapped.request_stream(
            messages, model_settings, model_request_parameters, run_context
        ) as stream:
            yield stream


async def test_load_failure_ends_the_run_with_the_hint():
    reg = RunRegistry()
    orch = build_chat_orchestrator("hi", model=_LoadFailingModel())
    run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch)
    await run.wait()

    assert run.status is RunStatus.error
    error = next(e.body for e in run.stream.replay() if e.body.type == "run.error")
    # The operator sees the actionable hint, not the raw "protocol startup aborted".
    assert "LM Studio" in error.message
    assert "qwen3.6-35b-a3b-mtp" in error.message
