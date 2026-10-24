"""A parked turn as JSON, and back.

What survives is the data the resume reads. The agent, the compaction context and the title
models are clients and handles, so the restored turn has none of them until its kind's
recompose hook (``recompose.py``) rebuilds them.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, JsonValue, TypeAdapter
from pydantic_ai import DeferredToolRequests, ToolApproved, ToolDenied
from pydantic_ai.messages import ModelMessagesTypeAdapter

from runs import Endpoint, Run
from services.conversations import ConversationBinding

from .gating import GrantApproved
from .parking import ParkedTurn

_REQUESTS = TypeAdapter(DeferredToolRequests)
_BINDING = TypeAdapter(ConversationBinding)


class StoredDecision(BaseModel):
    call_id: str
    kind: Literal["approved", "denied", "grant"]
    override_args: dict[str, JsonValue] | None = None
    message: str | None = None
    needs_whole_tool: bool = False


class StoredEndpoint(BaseModel):
    id: str
    name: str
    capacity: int | None = None


PAYLOAD_VERSION = 1


class ParkPayload(BaseModel):
    version: int
    kind: str
    # The history and the pending calls are written by their own pydantic-ai adapters, so
    # they travel as the JSON text those produce rather than as a second schema of ours.
    history: str
    requests: str
    binding: str
    announced: list[str]
    settled: list[StoredDecision]
    conversation_id: str | None
    prompt_message_id: str | None
    persist_from: int
    persist_from_parts: int
    clean_drop: list[int] | None
    vision: bool
    workspace_key: str
    titled: bool
    endpoint_id: str | None
    model: str | None
    endpoint: StoredEndpoint | None
    max_requests: int | None = None
    requests_used: int = 0
    ledger_prompt_message_id: str | None = None


def encode_park(parked: ParkedTurn, run: Run) -> str:
    """The park as JSON text. Raises when something in it can't be written as JSON."""
    settled: list[StoredDecision] = []
    for call_id, outcome in parked.settled.items():
        if isinstance(outcome, ToolDenied):
            settled.append(StoredDecision(call_id=call_id, kind="denied", message=outcome.message))
        else:
            kind = "grant" if isinstance(outcome, GrantApproved) else "approved"
            settled.append(
                StoredDecision(
                    call_id=call_id,
                    kind=kind,
                    override_args=outcome.override_args,
                    needs_whole_tool=isinstance(outcome, GrantApproved)
                    and outcome.needs_whole_tool,
                )
            )
    endpoint = run.endpoint
    return ParkPayload(
        version=PAYLOAD_VERSION,
        kind=run.kind,
        history=ModelMessagesTypeAdapter.dump_json(parked.message_history).decode(),
        requests=_REQUESTS.dump_json(parked.requests).decode(),
        binding=_BINDING.dump_json(parked.binding).decode(),
        announced=sorted(parked.announced),
        settled=settled,
        conversation_id=parked.conversation_id,
        prompt_message_id=parked.prompt_message_id,
        persist_from=parked.persist_from,
        persist_from_parts=parked.persist_from_parts,
        clean_drop=list(parked.clean_drop) if parked.clean_drop is not None else None,
        vision=parked.vision,
        workspace_key=parked.workspace_key,
        titled=parked.title is not None or parked.titled,
        endpoint_id=parked.endpoint_id,
        model=parked.model,
        endpoint=(
            StoredEndpoint(id=endpoint.id, name=endpoint.name, capacity=endpoint.capacity)
            if endpoint is not None
            else None
        ),
        max_requests=parked.max_requests,
        requests_used=parked.requests_used,
        ledger_prompt_message_id=parked.ledger_prompt_message_id,
    ).model_dump_json()


def decode_park(payload: str, *, kind: str) -> tuple[ParkedTurn, Endpoint | None]:
    """The park, without an agent, and the endpoint its run leased. Raises when the payload
    is another version, or sealed for a run of a kind other than ``kind``."""
    stored = ParkPayload.model_validate_json(payload)
    if stored.version != PAYLOAD_VERSION:
        raise ValueError(f"park payload version {stored.version} is not {PAYLOAD_VERSION}")
    if stored.kind != kind:
        raise ValueError(f"park sealed for a {stored.kind} run, stored as {kind}")
    settled: dict[str, ToolApproved | ToolDenied] = {}
    for each in stored.settled:
        if each.kind == "denied":
            settled[each.call_id] = (
                ToolDenied(message=each.message) if each.message else ToolDenied()
            )
        elif each.kind == "grant":
            settled[each.call_id] = GrantApproved(
                override_args=each.override_args, needs_whole_tool=each.needs_whole_tool
            )
        else:
            settled[each.call_id] = ToolApproved(override_args=each.override_args)
    parked = ParkedTurn(
        agent=None,
        message_history=ModelMessagesTypeAdapter.validate_json(stored.history),
        requests=_REQUESTS.validate_json(stored.requests),
        announced=set(stored.announced),
        settled=settled,
        conversation_id=stored.conversation_id,
        persist_from=stored.persist_from,
        persist_from_parts=stored.persist_from_parts,
        clean_drop=(stored.clean_drop[0], stored.clean_drop[1]) if stored.clean_drop else None,
        prompt_message_id=stored.prompt_message_id,
        binding=_BINDING.validate_json(stored.binding),
        vision=stored.vision,
        workspace_key=stored.workspace_key,
        endpoint_id=stored.endpoint_id,
        model=stored.model,
        titled=stored.titled,
        max_requests=stored.max_requests,
        requests_used=stored.requests_used,
        ledger_prompt_message_id=stored.ledger_prompt_message_id,
    )
    endpoint = (
        Endpoint(stored.endpoint.id, stored.endpoint.name, stored.endpoint.capacity)
        if stored.endpoint is not None
        else None
    )
    return parked, endpoint
