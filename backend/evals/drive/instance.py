"""A live dev instance as an eval drives it: bindings, endpoints, fixtures, turns, and the
session that proxies its endpoints for a run and puts everything back afterwards."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import shutil
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field, TypeAdapter

from devkit.instance import DevInstance
from runs.events.approvals import ApprovalRequired, QuestionAsked
from runs.events.union import EventBody

from .client import DevClient, Policy, Settler
from .proxy import Proxied, RecordingProxy
from .timeline import SubagentRecord, Timeline, Turn
from .usage import Kind, TokenWindow, Usage

_DELIVERY_POLL_S = 5.0
#: The run kind the inbox starts to hand a thread its sub-agents' reports.
WAKE_KIND = "wake"


class Binding(BaseModel):
    endpoint_ids: list[str]
    model: str | None = None
    #: Read only: the role is unbound and this is what the backend defaults it to.
    implicit: bool = False


class Endpoint(BaseModel):
    id: str
    base_url: str
    provider: str
    name: str = ""
    #: The endpoint's default model, None when it has none.
    model: str | None = None


class Hosted(BaseModel):
    """A hosted provider ``--provider`` can run on: where it lives and its key's name."""

    provider: str
    base_url: str
    key: str


HOSTED = {
    "anthropic": Hosted(
        provider="anthropic", base_url="https://api.anthropic.com", key="ANTHROPIC_API_KEY"
    )
}


class _Memory(BaseModel):
    id: str


class _Project(BaseModel):
    id: str


class _Card(BaseModel):
    handle: str
    callsign: str = ""
    agent_class: str = ""
    task: str
    status: str
    summary: str | None = None
    findings: object = None
    error: str | None = None
    name: str = ""
    conversation_id: str = ""
    parent_conversation_id: str = ""
    depth: int = 1
    model: str = ""
    model_source: str = ""


class _Cards(BaseModel):
    subagents: list[_Card]


class _InboxMessage(BaseModel):
    held: bool = False


class Inbox(BaseModel):
    messages: list[_InboxMessage] = Field(default_factory=list)
    agents_paused: bool = False
    #: The run live on the thread, if any.
    run_id: str | None = None


class _ChatSettings(BaseModel):
    work_summary_idle_minutes: int


class RunView(BaseModel):
    id: str
    kind: str
    status: str = ""
    created_at: datetime
    conversation_id: str | None = Field(default=None, alias="conversationId")


_BINDINGS: TypeAdapter[dict[str, Binding]] = TypeAdapter(dict[str, Binding])
_ENDPOINTS: TypeAdapter[list[Endpoint]] = TypeAdapter(list[Endpoint])
_RUNS: TypeAdapter[list[RunView]] = TypeAdapter(list[RunView])
_MEMORIES: TypeAdapter[list[_Memory]] = TypeAdapter(list[_Memory])


def prompt_fingerprint(prompt: str) -> str:
    return hashlib.sha256(prompt.encode()).hexdigest()[:12]


async def warm(base_url: str, model: str) -> None:
    """Load the model before the first scenario, so its first turn isn't timed on a cold load."""
    async with httpx.AsyncClient(timeout=600.0) as http:
        try:
            _ = await http.post(
                f"{base_url.rstrip('/')}/chat/completions",
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "ok"}],
                    "max_tokens": 1,
                },
            )
        except httpx.HTTPError as exc:
            print(f"  warm-up failed ({exc}); carrying on", flush=True)


def read[T](response: httpx.Response, shape: type[T] | TypeAdapter[T]) -> T:
    _ = response.raise_for_status()
    adapter = shape if isinstance(shape, TypeAdapter) else TypeAdapter(shape)
    return adapter.validate_json(response.content)


@dataclass(frozen=True, slots=True)
class Fixture:
    project_id: str
    #: ``~/projects/<slug>``, the path the model sees.
    home_path: str
    #: The copy on the host.
    path: Path


@dataclass(slots=True)
class Deliveries:
    """What waiting out a thread's sub-agents found."""

    turns: list[Turn] = field(default_factory=list)
    subagents: list[SubagentRecord] = field(default_factory=list)
    problem: str | None = None


type OnFrame = Callable[[EventBody], Awaitable[None]]


class Instance:
    """The routes an eval needs, against one dev instance."""

    def __init__(self, client: DevClient, instance: DevInstance) -> None:
        self.client: DevClient = client
        self.http: httpx.AsyncClient = client.http
        self.instance: DevInstance = instance

    async def bindings(self) -> dict[str, Binding]:
        return read(await self.http.get("/models/roles"), _BINDINGS)

    async def bind(self, role: str, binding: Binding) -> None:
        body = binding.model_dump(exclude={"implicit"})
        response = await self.http.put(f"/models/roles/{role}", json=body)
        _ = response.raise_for_status()

    async def endpoint(self, endpoint_id: str) -> Endpoint:
        return read(await self.http.get(f"/models/endpoints/{endpoint_id}"), Endpoint)

    async def endpoints(self) -> list[Endpoint]:
        return read(await self.http.get("/models/endpoints"), _ENDPOINTS)

    async def add_endpoint(
        self, hosted: Hosted, api_key: str, *, label: str = "voice-eval"
    ) -> Endpoint:
        body = {
            "name": f"{label} {hosted.provider}",
            "provider": hosted.provider,
            "base_url": hosted.base_url,
            "api_key": api_key,
            "vision": True,
        }
        return read(await self.http.post("/models/endpoints", json=body), Endpoint)

    async def remove_endpoint(self, endpoint_id: str) -> None:
        _ = (await self.http.delete(f"/models/endpoints/{endpoint_id}")).raise_for_status()

    async def point(self, endpoint_id: str, base_url: str) -> None:
        """Change only the endpoint's address; its key is left as it is."""
        response = await self.http.patch(
            f"/models/endpoints/{endpoint_id}", json={"base_url": base_url}
        )
        _ = response.raise_for_status()

    async def idle_minutes(self, minutes: int | None = None) -> int:
        """The work-summary idle window; sets it first when ``minutes`` is given."""
        if minutes is None:
            response = await self.http.get("/chat/settings")
        else:
            response = await self.http.put(
                "/chat/settings", json={"work_summary_idle_minutes": minutes}
            )
        return read(response, _ChatSettings).work_summary_idle_minutes

    async def memory_ids(self) -> set[str]:
        return {memory.id for memory in read(await self.http.get("/memory"), _MEMORIES)}

    async def runs(self) -> list[RunView]:
        """Every run the instance still holds, finished ones included."""
        return read(await self.http.get("/runs", params={"active": "false"}), _RUNS)

    async def fixture(self, source: Path, into: str) -> Fixture:
        """A fresh copy of ``source`` under ``<data dir>/<into>``, registered as a project.

        The copy's directory name is already a valid slug and unique, so the project's
        ``~/projects/<slug>`` is that name."""
        slug = f"{source.name}-{secrets.token_hex(3)}"
        target = self.instance.data_dir / into / slug
        _ = shutil.copytree(source, target)
        response = await self.http.post("/projects/ensure", json={"root_path": str(target)})
        return Fixture(read(response, _Project).id, f"~/projects/{slug}", target)

    async def follow(
        self,
        run_id: str,
        turn: Turn,
        policy: Policy,
        *,
        timeout_s: float,
        on_frame: OnFrame | None = None,
    ) -> Turn:
        """Record one run into ``turn``, answering what it parks on, until it ends."""
        turn.run_id = run_id
        timeline = Timeline(turn, policy)
        settler = Settler(self.client, run_id, policy)
        started = time.monotonic()

        async def follow() -> None:
            async for frame in self.client.frames(run_id):
                timeline.feed(frame.body)
                if isinstance(frame.body, ApprovalRequired | QuestionAsked):
                    settler.park(frame.body)
                if on_frame is not None:
                    await on_frame(frame.body)

        try:
            await asyncio.wait_for(follow(), timeout=timeout_s)
        except TimeoutError:
            turn.detail = f"no terminal event after {timeout_s:.0f}s"
        turn.wall_s = round(time.monotonic() - started, 1)
        return turn

    async def inbox(self, conversation_id: str) -> Inbox:
        return read(await self.http.get(f"/conversations/{conversation_id}/inbox"), Inbox)

    async def subagents(self, conversation_id: str) -> list[SubagentRecord]:
        cards = read(await self.http.get(f"/conversations/{conversation_id}/subagents"), _Cards)
        return [
            SubagentRecord(
                handle=card.handle,
                callsign=card.callsign,
                agent_class=card.agent_class,
                task=card.task,
                status=card.status,
                report=card.summary,
                findings=card.findings,
                error=card.error,
                name=card.name,
                conversation_id=card.conversation_id,
                parent_conversation_id=card.parent_conversation_id,
                depth=card.depth,
                model=card.model,
                model_source=card.model_source,
            )
            for card in cards.subagents
        ]

    async def wait_deliveries(
        self,
        conversation_id: str,
        policy: Policy,
        *,
        timeout_s: float,
        on_turn: Callable[[Turn], None] | None = None,
        followed: Iterable[str] = (),
    ) -> Deliveries:
        """Wait out the thread's sub-agents, recording each run that delivers a report.

        A report reaches the parent as a separate ``wake`` run the inbox starts once the
        child ends, so the reply that relays it is only seen by following those runs.

        Read cards, then inbox, then runs: a finished child's report sits in the inbox
        before its wake run exists. ``followed`` names runs already recorded elsewhere."""
        out = Deliveries()
        deadline = time.monotonic() + timeout_s
        seen = set(followed)
        quiet = 0
        while True:
            out.subagents = await self.subagents(conversation_id)
            inbox = await self.inbox(conversation_id)
            wakes = sorted(
                (
                    run
                    for run in await self.runs()
                    if run.kind == WAKE_KIND
                    and run.conversation_id == conversation_id
                    and run.id not in seen
                ),
                key=lambda run: run.created_at,
            )
            for run in wakes:
                seen.add(run.id)
                turn = await self.follow(
                    run.id, Turn(prompt="", delivery=True), policy, timeout_s=timeout_s
                )
                out.turns.append(turn)
                if on_turn is not None:
                    on_turn(turn)
            # A blocked child waits on its parent, which only answers from a turn; with no
            # turn running or due on the thread, nothing will answer it.
            running = [a for a in out.subagents if a.status == "running"]
            waiting = inbox.run_id is not None or any(not m.held for m in inbox.messages)
            if running or wakes or waiting:
                quiet = 0
            elif inbox.agents_paused:
                out.problem = "deliveries are paused until the operator's next turn"
                return out
            else:
                # Twice: a finished child's card settles a moment before its report is
                # queued, so one quiet read can fall in between. With no child there is no
                # report to wait for.
                quiet += 1
                if quiet >= 2 or not out.subagents:
                    return out
            if time.monotonic() > deadline:
                out.problem = f"sub-agents still live after {timeout_s:.0f}s"
                return out
            await asyncio.sleep(_DELIVERY_POLL_S)


class NoMainBinding(Exception):
    """The instance has no ``main`` binding to run on and no hosted endpoint was asked for."""


@dataclass(slots=True)
class Session:
    """A run's view of the instance's endpoints, each behind its own recording proxy."""

    endpoint: Endpoint
    #: Added for the run and deleted after it.
    temporary: bool
    stub: bool
    #: The role bindings before the run, restored after it.
    original: dict[str, Binding]
    #: By endpoint id; the endpoint under test is always one.
    proxies: dict[str, RecordingProxy]
    window: TokenWindow

    @property
    def proxy(self) -> RecordingProxy:
        return self.proxies[self.endpoint.id]

    def attribute(self, bucket: str) -> None:
        for proxy in self.proxies.values():
            proxy.attribute(bucket)

    def watch(self, texts: tuple[str, ...]) -> None:
        for proxy in self.proxies.values():
            proxy.watch(texts)

    def take(self, bucket: str) -> dict[Kind, Usage]:
        out: dict[Kind, Usage] = {}
        for proxy in self.proxies.values():
            for kind, usage in proxy.take(bucket).items():
                out.setdefault(kind, Usage()).merge(usage)
        return out

    def take_requests(self, bucket: str) -> list[Proxied]:
        return [r for p in self.proxies.values() for r in p.take_requests(bucket)]


@asynccontextmanager
async def session(
    driver: Instance,
    *,
    hosted: Hosted | None = None,
    api_key: str | None = None,
    tokens_per_minute: int | None = None,
    label: str = "voice-eval",
    also: Sequence[str] = (),
    restore: Sequence[str] = ("main", "utility"),
    idle_minutes: int = 1,
) -> AsyncIterator[Session]:
    """Proxy the endpoint under test, and each of ``also``, for the length of the block.

    The endpoint under test is a temporary one for ``hosted`` or the head of ``main``. The
    work-summary idle window is set to ``idle_minutes``. On the way out every endpoint is
    pointed back, each role in ``restore`` rebound as it was (or unbound), the temporary
    endpoint deleted and the idle window restored."""
    original = await driver.bindings()
    if not hosted and ("main" not in original or not original["main"].endpoint_ids):
        raise NoMainBinding("The instance has no main binding to run on.")
    idle_before = await driver.idle_minutes()
    if hosted and api_key:
        endpoint = await driver.add_endpoint(hosted, api_key, label=label)
        temporary = True
    else:
        endpoint = await driver.endpoint(original["main"].endpoint_ids[0])
        temporary = False
    window = TokenWindow(tokens_per_minute or sys.maxsize)
    targets = [endpoint]
    for endpoint_id in also:
        if endpoint_id not in {e.id for e in targets}:
            targets.append(await driver.endpoint(endpoint_id))
    proxies = {e.id: RecordingProxy(e.base_url, tag=e.name or e.id, window=window) for e in targets}
    stub = urlparse(endpoint.base_url).port == driver.instance.stub_port
    pointed: list[Endpoint] = []
    try:
        for target in targets:
            await proxies[target.id].start()
            await driver.point(target.id, proxies[target.id].base_url)
            # The temporary endpoint is deleted on the way out, so it is never pointed back.
            if not (temporary and target.id == endpoint.id):
                pointed.append(target)
        _ = await driver.idle_minutes(idle_minutes)
        yield Session(endpoint, temporary, stub, original, proxies, window)
    finally:
        for target in pointed:
            await driver.point(target.id, target.base_url)
        for proxy in proxies.values():
            await proxy.stop()
        for role in restore:
            was = original.get(role)
            # An implicit binding is the backend's default; rebinding it would pin it.
            if was is None or was.implicit:
                was = Binding(endpoint_ids=[])
            await driver.bind(role, was)
        if temporary:
            await driver.remove_endpoint(endpoint.id)
        _ = await driver.idle_minutes(idle_before)
