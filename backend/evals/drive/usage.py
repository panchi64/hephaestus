"""Token use and cost of the model calls a run makes, read off the Anthropic responses the
recording proxy relays.

Usage goes through Pydantic AI's own Anthropic mapping and is priced by the platform's
``response_cost``, so a run's cost is the figure the platform's stats would show.
Responses from other providers carry no Anthropic usage events and aren't counted.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import ClassVar, Literal

from anthropic.types.beta import BetaMessage, BetaRawMessageDeltaEvent, BetaRawMessageStartEvent
from pydantic import BaseModel, ConfigDict, ValidationError
from pydantic_ai import ModelResponse
from pydantic_ai.models.anthropic._response import map_usage
from pydantic_ai.usage import RequestUsage

from services.conversations.usage import response_cost
from services.pricing import Cost

#: ``agent`` is a call that carried tools (Talos or a sub-agent); ``utility`` is one that
#: didn't (titles, work summaries).
type Kind = Literal["agent", "utility"]

_PROVIDER = "anthropic"
_WINDOW_S = 60.0


class Usage(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    requests: int = 0
    #: Every prompt token, cached or not, as Pydantic AI counts them.
    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    #: What caching saved against sending every prompt uncached; negative when the
    #: cache writes cost more than the reads saved.
    saved_usd: float = 0.0
    #: Requests the price table had no price for: their tokens count, their cost doesn't.
    unpriced: int = 0

    def add(self, usage: RequestUsage, cost: Cost | None) -> None:
        self.requests += 1
        self.input_tokens += usage.input_tokens
        self.cache_read_tokens += usage.cache_read_tokens
        self.cache_write_tokens += usage.cache_write_tokens
        self.output_tokens += usage.output_tokens
        if cost is None:
            self.unpriced += 1
        else:
            self.cost_usd += cost.total_usd
            self.saved_usd += cost.saved_usd

    def merge(self, other: Usage) -> None:
        for name in type(self).model_fields:
            setattr(self, name, getattr(self, name) + getattr(other, name))

    @property
    def cache_read_share(self) -> float:
        return self.cache_read_tokens / self.input_tokens if self.input_tokens else 0.0


class _Event(BaseModel):
    type: str = ""


def total(parts: Iterable[Usage]) -> Usage:
    out = Usage()
    for part in parts:
        out.merge(part)
    return out


def price(usage: RequestUsage, model: str, provider_url: str) -> Cost | None:
    response = ModelResponse(
        parts=[],
        usage=usage,
        model_name=model,
        provider_url=provider_url,
        timestamp=datetime.now(UTC),
    )
    return response_cost(response)


class ResponseUsage:
    """Reads one relayed response's usage as its bytes pass through.

    A stream reports usage in ``message_start`` and then cumulatively in each
    ``message_delta``; a plain response carries it in the message body. Anything it
    can't read is skipped: the relay must never fail on accounting.
    """

    def __init__(self, provider_url: str, *, stream: bool, on_start: Callable[[int], None]) -> None:
        self._url: str = provider_url
        self._stream: bool = stream
        self._on_start: Callable[[int], None] = on_start
        self._buffer: bytes = b""
        self._usage: RequestUsage | None = None
        self._model: str = ""

    def feed(self, chunk: bytes) -> None:
        self._buffer += chunk
        if not self._stream:
            return
        *events, self._buffer = self._buffer.split(b"\n\n")
        for event in events:
            for line in event.splitlines():
                if line.startswith(b"data:"):
                    self._event(line.removeprefix(b"data:").strip())

    def _event(self, data: bytes) -> None:
        try:
            kind = _Event.model_validate_json(data).type
            if kind == "message_start":
                start = BetaRawMessageStartEvent.model_validate_json(data)
                self._model = start.message.model
                usage = map_usage(start, _PROVIDER, self._url, self._model)
                self._usage = usage
                self._on_start(usage.input_tokens)
            elif kind == "message_delta" and self._usage is not None:
                delta = BetaRawMessageDeltaEvent.model_validate_json(data)
                self._usage = map_usage(delta, _PROVIDER, self._url, self._model, self._usage)
        except ValidationError:
            return

    def finish(self) -> tuple[RequestUsage, Cost | None] | None:
        """The response's usage and price, or None when it reported none."""
        if not self._stream:
            try:
                message = BetaMessage.model_validate_json(self._buffer)
            except ValidationError:
                return None
            self._model = message.model
            usage = map_usage(message, _PROVIDER, self._url, self._model)
            self._usage = usage
            self._on_start(usage.input_tokens)
        if self._usage is None:
            return None
        return self._usage, price(self._usage, self._model, self._url)


class TokenWindow:
    """Prompt tokens sent in the last minute, to stay under a tokens-per-minute limit.

    Counts every prompt token, cached ones included, which is at or above what a
    rate limit counts. It reacts to responses already started, so the requests in
    flight when it fills can overshoot it: set it below the real limit.
    """

    def __init__(self, per_minute: int, clock: Callable[[], float] = time.monotonic) -> None:
        self.per_minute: int = per_minute
        self._clock: Callable[[], float] = clock
        self._sent: deque[tuple[float, int]] = deque()
        self.peak: int = 0

    def _prune(self, now: float) -> int:
        while self._sent and self._sent[0][0] <= now - _WINDOW_S:
            _ = self._sent.popleft()
        return sum(tokens for _, tokens in self._sent)

    def add(self, tokens: int) -> None:
        now = self._clock()
        self._sent.append((now, tokens))
        self.peak = max(self.peak, self._prune(now))

    def wait_s(self) -> float:
        """How long the next request should wait; 0 when the window has room."""
        now = self._clock()
        if self._prune(now) < self.per_minute:
            return 0.0
        return self._sent[0][0] + _WINDOW_S - now
