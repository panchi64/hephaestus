"""A run context that keeps what a tool emits, for tests that call tools directly."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TypeVar, overload, override

from pydantic_ai import RunContext
from pydantic_ai.messages import CapabilityEvent, CustomEvent
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from core.container import ServiceContainer
from runs import Run, RunStream
from tools import RunDeps
from tools.emit import RunEventEmitted

_CustomT = TypeVar("_CustomT", bound=CustomEvent)
_CapabilityT = TypeVar("_CapabilityT", bound=CapabilityEvent)


@dataclass
class RecordingContext(RunContext[RunDeps]):
    """A context with no agent run behind it, so what a tool emits is kept for the test."""

    emitted: list[object] = field(default_factory=list[object])

    @overload
    async def emit(self, event: _CustomT, /) -> _CustomT: ...

    @overload
    async def emit(self, event: _CapabilityT, /) -> _CapabilityT: ...

    @override
    async def emit(self, event: CustomEvent | CapabilityEvent, /) -> CustomEvent | CapabilityEvent:
        if isinstance(event, RunEventEmitted):
            self.emitted.append(event.body)
        return event


def recording_context(
    caps: ServiceContainer, conversation_id: str, *, owner_id: str
) -> RecordingContext:
    run = Run(id=f"run-{conversation_id}", kind="chat", owner_id=owner_id, stream=RunStream())
    deps = RunDeps(run=run, owner_id=owner_id, caps=caps, conversation_id=conversation_id)
    return RecordingContext(deps=deps, model=TestModel(), usage=RunUsage())
