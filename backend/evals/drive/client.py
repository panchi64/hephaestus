"""The dev instance's HTTP API, as much of it as an eval needs: send, steer, follow, settle."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx
from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from runs.events.approvals import ApprovalRequired, QuestionAsked
from runs.events.union import EventBody

_EVENT: TypeAdapter[EventBody] = TypeAdapter(EventBody)

#: How long a settle waits for the run to actually park after the first approval event.
_PARK_WAIT_S = 30.0

#: Tools whose effects stay inside the instance's VM and data dir. An eval runs unattended,
#: so anything else (the operator's machine, mail, calendar, vault, browser) is refused
#: unless the scenario allows its prefix.
_VM_TOOL_PREFIXES = ("shell_", "files_", "code_execute", "plan_", "tasks_", "project_")

#: Worded as an operator's decline, so the reply under grading doesn't relay eval plumbing.
_HOST_REFUSAL = "Declined. Work from what you can reach in your own workspace."
_POLICY_REFUSAL = "Not allowed in this task; find another way."


def eval_may_approve(body: ApprovalRequired, allow: tuple[str, ...] = ()) -> bool:
    """Whether an unattended eval may approve this call: VM-only tools and the ``allow``
    prefixes, never host reach."""
    prefixes = _VM_TOOL_PREFIXES + allow
    return body.name.startswith(prefixes) and body.args.get("reach") != "host"


@dataclass(frozen=True, slots=True)
class Policy:
    """How an unattended eval answers what a run parks on."""

    #: False refuses every parked call.
    approve: bool = True
    #: Tool prefixes approvable beyond the VM-only ones.
    allow: tuple[str, ...] = ()
    #: What every question is answered with; None for a note to use its judgement.
    answer: str | None = None
    #: Tool prefixes refused even when ``approve`` would allow them.
    deny: tuple[str, ...] = ()
    #: Regexes searched in a call's JSON-encoded arguments, refused like ``deny``.
    deny_args: tuple[str, ...] = ()
    #: The note every refusal carries instead of the defaults, which invite another route.
    refusal: str | None = None


class _Created(BaseModel):
    run_id: str
    conversation_id: str


class _Queued(BaseModel):
    conversation_id: str
    queued_message_id: str
    run_id: str | None = None


@dataclass(frozen=True, slots=True)
class Started:
    run_id: str
    conversation_id: str


@dataclass(frozen=True, slots=True)
class Queued:
    """The thread was busy: the message went into its inbox for the running turn."""

    conversation_id: str
    message_id: str
    run_id: str | None


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
    """One client per eval run; the instance runs without auth, so no token is sent."""

    def __init__(self, base_url: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._http: httpx.AsyncClient = httpx.AsyncClient(
            base_url=base_url, timeout=httpx.Timeout(30.0, read=None), transport=transport
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    @property
    def http(self) -> httpx.AsyncClient:
        """The underlying client, for routes this class has no method for."""
        return self._http

    async def post(
        self,
        prompt: str,
        conversation_id: str | None,
        *,
        permission_level: str | None = None,
        project_id: str | None = None,
    ) -> Started | Queued:
        """Send a message: a new turn, or a queued one when the thread's turn is running."""
        body: dict[str, object] = {"prompt": prompt}
        if conversation_id is not None:
            body["conversation_id"] = conversation_id
        if permission_level is not None:
            body["permission_level"] = permission_level
        if project_id is not None:
            body["project_id"] = project_id
        response = await self._http.post("/chat", json=body)
        _ = response.raise_for_status()
        # Queued first: its reply carries a `run_id` too, so it would also read as created.
        try:
            queued = _Queued.model_validate_json(response.content)
        except ValidationError:
            created = _Created.model_validate_json(response.content)
            return Started(created.run_id, created.conversation_id)
        return Queued(queued.conversation_id, queued.queued_message_id, queued.run_id)

    async def send(
        self,
        prompt: str,
        conversation_id: str | None,
        *,
        permission_level: str | None = None,
        project_id: str | None = None,
    ) -> tuple[str, str]:
        """Start a turn; returns ``(run_id, conversation_id)``. Raises when the thread was
        busy and the message was queued instead."""
        sent = await self.post(
            prompt, conversation_id, permission_level=permission_level, project_id=project_id
        )
        if isinstance(sent, Queued):
            raise RuntimeError(f"{sent.conversation_id} was busy; the message was queued")
        return sent.run_id, sent.conversation_id

    async def cancel(self, run_id: str) -> None:
        _ = (await self._http.post(f"/runs/{run_id}/cancel")).raise_for_status()

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
        approvals: list[ApprovalRequired],
        questions: dict[str, int],
        policy: Policy,
    ) -> None:
        """Answer everything the run parked on, once it has parked.

        Under ``policy.approve`` a call is still refused when :func:`eval_may_approve`
        rejects it. ``questions`` maps each ``ask_user`` call to how many questions it
        asked; each is answered with ``policy.answer``, or a note to use its judgement.
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
        reply = _Reply() if policy.answer is None else _Reply(text=policy.answer)
        body = _Settlement(
            decisions=[_decide(call, policy) for call in approvals],
            answers=[
                _Answer(tool_call_id=call_id, replies=[reply] * count)
                for call_id, count in questions.items()
            ],
        )
        response = await self._http.post(
            f"/runs/{run_id}/approve", content=body.model_dump_json(), headers=_JSON
        )
        _ = response.raise_for_status()


def refusal(call: ApprovalRequired, *, approve: bool, allow: tuple[str, ...] = ()) -> str | None:
    """The note a parked call is refused with, or None when it is approved."""
    if not approve:
        return _POLICY_REFUSAL
    if not eval_may_approve(call, allow):
        return _HOST_REFUSAL
    return None


def policy_refusal(call: ApprovalRequired, policy: Policy) -> str | None:
    """:func:`refusal` under a whole :class:`Policy`: its denied prefixes and its note."""
    if (policy.deny and call.name.startswith(policy.deny)) or _args_denied(call, policy):
        message: str | None = _POLICY_REFUSAL
    else:
        message = refusal(call, approve=policy.approve, allow=policy.allow)
    return policy.refusal if message is not None and policy.refusal else message


def _args_denied(call: ApprovalRequired, policy: Policy) -> bool:
    if not policy.deny_args:
        return False
    args = json.dumps(call.args)
    return any(re.search(pattern, args) for pattern in policy.deny_args)


def _decide(call: ApprovalRequired, policy: Policy) -> _Decision:
    message = policy_refusal(call, policy)
    return _Decision(tool_call_id=call.tool_call_id, approved=message is None, message=message)


class Settler:
    """Answers whatever one run parks on, batch by batch, under one approval policy."""

    def __init__(self, client: DevClient, run_id: str, policy: Policy) -> None:
        self._client: DevClient = client
        self._run_id: str = run_id
        self._policy: Policy = policy
        self._approvals: list[ApprovalRequired] = []
        self._questions: dict[str, int] = {}
        self._task: asyncio.Task[None] | None = None

    def park(self, body: ApprovalRequired | QuestionAsked) -> None:
        if isinstance(body, ApprovalRequired):
            self._approvals.append(body)
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
            await self._client.settle(self._run_id, approvals, questions, self._policy)
