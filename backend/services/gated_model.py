"""A model whose requests take a slot of their endpoint's line (`runs/endpoint_gate.py`).

Only a chain's primary is wrapped: a fallback endpoint is reached on failure, not load, and
waiting on it from inside a run that holds another endpoint could close a cycle.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import override

from pydantic_ai import RunContext
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models import Model, ModelRequestParameters, StreamedResponse
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ModelSettings

from runs import Endpoint, EndpointGate


@dataclass(init=False)
class EndpointGatedModel(WrapperModel):
    gate: EndpointGate
    endpoint: Endpoint

    def __init__(self, wrapped: Model, gate: EndpointGate, endpoint: Endpoint) -> None:
        super().__init__(wrapped)
        self.gate = gate
        self.endpoint = endpoint

    @override
    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        async with self.gate.request(self.endpoint):
            return await self.wrapped.request(messages, model_settings, model_request_parameters)

    @override
    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: RunContext[object] | None = None,
    ) -> AsyncGenerator[StreamedResponse]:
        async with (
            self.gate.request(self.endpoint),
            self.wrapped.request_stream(
                messages, model_settings, model_request_parameters, run_context
            ) as stream,
        ):
            yield stream
