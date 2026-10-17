"""The dev instance's HTTP API, as much of it as a task needs: send, follow, settle."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx
from pydantic import BaseModel, Field, TypeAdapter

from runs.events.approvals import ApprovalRequired, QuestionAsked
from runs.events.union import EventBody

_EVENT: TypeAdapter[EventBody] = TypeAdapter(EventBody)

#: How long a settle waits for the run to actually park after the first approval event.
_PARK_WAIT_S = 30.0


class _Created(BaseModel):
    run_id: str
    conversation_id: str


class _RunStatus(BaseModel):
    status: str


class _Stamp(BaseModel):
    seq: int


class _Decision(BaseModel):
    tool_call_id: str
    approved: bool
    message: str | None = None


class _Reply(BaseModel):
    selections: list[str] = Field(default_factory=list)
    text: str = "Use your best judgement and carry on."


class _Answer(BaseModel):
    tool_call_id: str
    replies: list[_Reply]


class _Settlement(BaseModel):
    decisions: list[_Decision]
    answers: list[_Answer]


_JSON = {"content-type": "application/json"}


@dataclass(frozen=True, slots=True)
class Frame:
    seq: int
    body: EventBody


class DevClient:
    """One client per task run; the instance runs without auth, so no token is sent."""

    def __init__(self, base_url: str) -> None:
        self._http: httpx.AsyncClient = httpx.AsyncClient(
            base_url=base_url, timeout=httpx.Timeout(30.0, read=None)
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    @property
    def http(self) -> httpx.AsyncClient:
        """The underlying client, for routes this class has no method for."""
        return self._http

    async def send(
        self,
        prompt: str,
        conversation_id: str | None,
        *,
        permission_level: str | None = None,
        project_id: str | None = None,
    ) -> tuple[str, str]:
        """Start a turn; returns ``(run_id, conversation_id)``."""
        body: dict[str, object] = {"prompt": prompt}
        if conversation_id is not None:
            body["conversation_id"] = conversation_id
        if permission_level is not None:
            body["permission_level"] = permission_level
        if project_id is not None:
            body["project_id"] = project_id
        response = await self._http.post("/chat", json=body)
        _ = response.raise_for_status()
        created = _Created.model_validate_json(response.content)
        return created.run_id, created.conversation_id

    async def frames(self, run_id: str) -> AsyncIterator[Frame]:
        """Every event of the run, reconnecting from the last seq until the stream ends.

        The server closes the stream when the run reaches a terminal state, so a clean
        end of the response is the end of the run.
        """
        last = 0
        while True:
            try:
                async with self._http.stream(
                    "GET", f"/runs/{run_id}/events", params={"last_event_id": last}
                ) as response:
                    _ = response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        raw = line.removeprefix("data:").strip()
                        last = _Stamp.model_validate_json(raw).seq
                        yield Frame(seq=last, body=_EVENT.validate_json(raw))
                return
            except httpx.TransportError:
                await asyncio.sleep(1.0)

    async def settle(
        self,
        run_id: str,
        approvals: list[str],
        questions: dict[str, int],
        *,
        approve: bool,
    ) -> None:
        """Answer everything the run parked on, once it has parked.

        ``questions`` maps each ``ask_user`` call to how many questions it asked; each is
        answered with the model's first offered option, or a note to use its judgement.
        """
        deadline = asyncio.get_running_loop().time() + _PARK_WAIT_S
        while True:
            response = await self._http.get(f"/runs/{run_id}")
            _ = response.raise_for_status()
            if _RunStatus.model_validate_json(response.content).status == "awaiting_input":
                break
            if asyncio.get_running_loop().time() > deadline:
                return
            await asyncio.sleep(0.5)
        refusal = None if approve else "Not allowed in this task; find another way."
        body = _Settlement(
            decisions=[
                _Decision(tool_call_id=call_id, approved=approve, message=refusal)
                for call_id in approvals
            ],
            answers=[
                _Answer(tool_call_id=call_id, replies=[_Reply()] * count)
                for call_id, count in questions.items()
            ],
        )
        response = await self._http.post(
            f"/runs/{run_id}/approve", content=body.model_dump_json(), headers=_JSON
        )
        _ = response.raise_for_status()


class Settler:
    """Answers whatever one run parks on, batch by batch, under one approval policy."""

    def __init__(self, client: DevClient, run_id: str, *, approve: bool) -> None:
        self._client: DevClient = client
        self._run_id: str = run_id
        self._approve: bool = approve
        self._approvals: list[str] = []
        self._questions: dict[str, int] = {}
        self._task: asyncio.Task[None] | None = None

    def park(self, body: ApprovalRequired | QuestionAsked) -> None:
        if isinstance(body, ApprovalRequired):
            self._approvals.append(body.tool_call_id)
        else:
            self._questions[body.tool_call_id] = len(body.questions)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._settle())

    async def _settle(self) -> None:
        # Until nothing is waiting: a resumed run can park again before the answer to the
        # last batch returns, and no new settle starts while this one runs.
        while self._approvals or self._questions:
            # Let the rest of the batch arrive before answering it as one.
            await asyncio.sleep(0.5)
            approvals = self._approvals.copy()
            questions = dict(self._questions)
            self._approvals.clear()
            self._questions.clear()
            await self._client.settle(self._run_id, approvals, questions, approve=self._approve)
