"""One sample of one scenario: set it up, drive its turns, follow its sub-agents, score it."""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

import httpx
from pydantic import BaseModel, TypeAdapter, ValidationError

from evals.drive.client import Policy, Queued
from evals.drive.instance import Fixture, Instance, read
from evals.drive.timeline import Turn
from runs.events.tools import ToolStarted
from runs.events.union import EventBody
from services.sandbox.base import safe_key

from .checks import evaluate
from .evidence import Evidence, ProxiedRequest
from .record import Sample
from .routing import Router
from .spec import FIXTURES_DIR, PROJECT_TOKEN, Action, Scenario, fill

_WATCH_POLL_S = 2.0
_LIVE = frozenset({"running", "blocked"})
_TITLE_WAIT_S = 30.0


class _Conversation(BaseModel):
    title: str | None = None


class _Task(BaseModel):
    content: str
    status: str


_TASKS: TypeAdapter[list[_Task]] = TypeAdapter(list[_Task])


class _Event(BaseModel):
    id: str


class _Events(BaseModel):
    items: list[_Event]


#: What every refusal says: a decline, never an invitation to try another route.
_REFUSAL = "Declined. Leave it as it is."


def policy_of(scenario: Scenario) -> Policy:
    return Policy(
        approve=scenario.approve == "all",
        allow=tuple(scenario.allow),
        answer=scenario.answer,
        deny=tuple(scenario.deny),
        deny_args=tuple(scenario.deny_args),
        refusal=_REFUSAL,
    )


class Actor:
    """Fires a turn's actions when their trigger matches, each once."""

    def __init__(
        self, driver: Instance, actions: list[Action], conversation_id: str, log: list[str]
    ) -> None:
        self._driver: Instance = driver
        self._pending: list[Action] = list(actions)
        self._conversation_id: str = conversation_id
        self._log: list[str] = log
        self._timers: list[asyncio.Task[None]] = []
        self.run_id: str = ""
        self.cancelled: bool = False

    def start(self, run_id: str) -> None:
        self.run_id = run_id
        for action in [a for a in self._pending if a.at.startswith("seconds:")]:
            delay = float(action.at.removeprefix("seconds:"))
            self._timers.append(asyncio.create_task(self._after(delay, action)))

    async def _after(self, delay: float, action: Action) -> None:
        await asyncio.sleep(delay)
        await self._fire(action)

    async def on_frame(self, body: EventBody) -> None:
        if not isinstance(body, ToolStarted):
            return
        for action in list(self._pending):
            if action.at == "first_tool" or (
                action.at.startswith("tool:") and body.name.startswith(action.at[5:])
            ):
                await self._fire(action)

    async def _fire(self, action: Action) -> None:
        if action not in self._pending:
            return
        self._pending.remove(action)
        # The turn can end between the trigger and the request; that is logged, not fatal.
        try:
            if action.do == "cancel":
                await self._driver.client.cancel(self.run_id)
                self.cancelled = True
                self._log.append(f"cancelled the turn at {action.at}")
                return
            sent = await self._driver.client.post(action.text, self._conversation_id)
        except httpx.HTTPError as exc:
            self._log.append(f"{action.do} at {action.at} failed: {exc}")
            return
        landed = "queued into the running turn" if isinstance(sent, Queued) else "started a turn"
        self._log.append(f"steered at {action.at}: {action.text!r} ({landed})")

    def stop(self) -> None:
        for timer in self._timers:
            _ = timer.cancel()


@dataclass(slots=True)
class Watcher:
    """Follows every run of every sub-agent below the watched threads, live, so a child's
    own timeline is recorded and what it parks on is answered."""

    driver: Instance
    policy: Policy
    timeout_s: float
    roots: set[str] = field(default_factory=set)
    turns: dict[str, list[tuple[float, Turn]]] = field(default_factory=dict)
    _followed: set[str] = field(default_factory=set)
    _children: set[str] = field(default_factory=set)
    _follows: list[asyncio.Task[Turn]] = field(default_factory=list)
    _loop: asyncio.Task[None] | None = None

    def watch(self, conversation_id: str) -> None:
        self.roots.add(conversation_id)
        if self._loop is None:
            self._loop = asyncio.create_task(self._poll())

    async def _poll(self) -> None:
        while True:
            with contextlib.suppress(httpx.HTTPError, ValidationError):
                await self.scan()
            await asyncio.sleep(_WATCH_POLL_S)

    async def scan(self, *, full: bool = False) -> None:
        """Follow child runs not followed yet. The instance's whole run list is read only
        when a child is new or live, or for ``full``: a settled tree starts no runs."""
        cards = [card for root in list(self.roots) for card in await self.driver.subagents(root)]
        children = {card.conversation_id for card in cards}
        changed = children != self._children or any(c.status in _LIVE for c in cards)
        self._children = children
        if not (changed or full):
            return
        for run in await self.driver.runs():
            if run.conversation_id in children and run.id not in self._followed:
                self._followed.add(run.id)
                turn = Turn(prompt="")
                stamp = run.created_at.timestamp()
                self.turns.setdefault(run.conversation_id, []).append((stamp, turn))
                follow = self.driver.follow(run.id, turn, self.policy, timeout_s=self.timeout_s)
                self._follows.append(asyncio.create_task(follow))

    async def finish(self) -> dict[str, list[Turn]]:
        if self._loop is not None:
            _ = self._loop.cancel()
        with contextlib.suppress(httpx.HTTPError, ValidationError):
            await self.scan(full=True)
        _ = await asyncio.gather(*self._follows, return_exceptions=True)
        return {
            cid: [t for _, t in sorted(runs, key=lambda r: r[0])]
            for cid, runs in self.turns.items()
        }


@dataclass(frozen=True, slots=True)
class Context:
    """What every sample of a pass shares."""

    router: Router
    stub_url: str | None
    #: ``{local}`` and ``{hosted}``.
    tokens: dict[str, str]


class Harness(Instance):
    async def _git(self, path: Path) -> None:
        for args in (
            ("init", "-q"),
            ("add", "-A"),
            ("-c", "user.name=eval", "-c", "user.email=eval@localhost", "commit", "-qm", "fixture"),
        ):
            process = await asyncio.create_subprocess_exec("git", *args, cwd=path)
            if await process.wait() != 0:
                raise RuntimeError(f"git {args[-1]} failed in the fixture copy")

    async def _script(self, stub_url: str, scenario: Scenario, tokens: dict[str, str]) -> None:
        _ = (await self.http.post(f"{stub_url}/_stub/reset")).raise_for_status()
        body = [reply.wire(tokens, index) for index, reply in enumerate(scenario.stub)]
        _ = (await self.http.post(f"{stub_url}/_stub/script", json=body)).raise_for_status()

    async def _thread(
        self,
        scenario: Scenario,
        prompts: list[str],
        tokens: dict[str, str],
        project_id: str | None,
        watcher: Watcher,
        record: Sample,
        into: list[Turn],
        cancelled: set[int],
    ) -> str | None:
        """Send ``prompts`` in a thread of their own, recording each turn into ``into``."""
        policy = policy_of(scenario)
        conversation_id: str | None = None
        for number, raw in enumerate(prompts, start=1):
            text = fill(raw, tokens)
            if conversation_id is not None:
                await self._idle(conversation_id, policy, scenario.timeout_s, into)
            sent = await self.client.post(
                text,
                conversation_id,
                permission_level=scenario.permission_level,
                project_id=project_id if conversation_id is None else None,
            )
            if isinstance(sent, Queued):
                record.problem = f"turn {number} was queued behind a run that started meanwhile"
                break
            run_id = sent.run_id
            conversation_id = sent.conversation_id
            watcher.watch(conversation_id)
            actions = [a for a in scenario.actions if a.turn == number and into is record.turns]
            actor = Actor(self, actions, conversation_id, record.actions)
            actor.start(run_id)
            try:
                turn = await self.follow(
                    run_id,
                    Turn(prompt=text),
                    policy,
                    timeout_s=scenario.timeout_s,
                    on_frame=actor.on_frame,
                )
            finally:
                actor.stop()
            if actor.cancelled:
                cancelled.add(len(into))
            into.append(turn)
            if turn.ended != "done" and not actor.cancelled:
                record.problem = f"turn {number} ended {turn.ended}"
                break
        if conversation_id is not None:
            found = await self.wait_deliveries(
                conversation_id,
                policy,
                timeout_s=scenario.timeout_s,
                on_turn=into.append,
                followed={t.run_id for t in into},
            )
            record.subagents += found.subagents
            if found.problem:
                record.problem = found.problem
        return conversation_id

    async def _idle(
        self, conversation_id: str, policy: Policy, timeout_s: float, into: list[Turn]
    ) -> None:
        """Follow whatever runs on the thread before the next prompt is sent, so it lands
        as a turn of its own: a sub-agent's report can start a wake run in between."""
        deadline = time.monotonic() + timeout_s
        while (run_id := (await self.inbox(conversation_id)).run_id) is not None:
            if time.monotonic() > deadline:
                return
            if run_id in {t.run_id for t in into}:
                # Already followed to its end; the thread just hasn't cleared it yet.
                await asyncio.sleep(0.5)
                continue
            delivery = Turn(prompt="", delivery=True)
            into.append(await self.follow(run_id, delivery, policy, timeout_s=timeout_s))

    async def _event_ids(self) -> set[str]:
        return {e.id for e in read(await self.http.get("/calendar/events"), _Events).items}

    async def _title(self, conversation_id: str) -> str | None:
        deadline = time.monotonic() + _TITLE_WAIT_S
        while True:
            found = read(await self.http.get(f"/conversations/{conversation_id}"), _Conversation)
            if found.title or time.monotonic() > deadline:
                return found.title
            await asyncio.sleep(2.0)

    async def sample(
        self, scenario: Scenario, number: int, context: Context
    ) -> tuple[Sample, Evidence]:
        record = Sample(scenario=scenario.id, sample=number)
        memories_before: set[str] | None = None
        events_before: set[str] | None = None
        copy: Fixture | None = None
        cancelled: set[int] = set()
        watcher = Watcher(self, policy_of(scenario), scenario.timeout_s)
        child_turns: dict[str, list[Turn]] = {}
        try:
            memories_before = await self.memory_ids()
            events_before = await self._event_ids()
            if scenario.routing:
                await context.router.apply(scenario.routing)
            for memory in scenario.memories:
                _ = (await self.http.post("/memory", json=memory.model_dump())).raise_for_status()
            if scenario.fixture:
                copy = await self.fixture(FIXTURES_DIR / scenario.fixture, "harness-fixtures")
                if scenario.git:
                    await self._git(copy.path)
            tokens = {**context.tokens, PROJECT_TOKEN: copy.home_path if copy else ""}
            if context.stub_url:
                await self._script(context.stub_url, scenario, tokens)
            project_id = copy.project_id if copy else None
            record.conversation_id = await self._thread(
                scenario,
                scenario.prompts,
                tokens,
                project_id,
                watcher,
                record,
                record.turns,
                cancelled,
            )
            if scenario.new_thread and record.problem is None:
                record.later_conversation_id = await self._thread(
                    scenario,
                    scenario.new_thread,
                    tokens,
                    project_id,
                    watcher,
                    record,
                    record.later,
                    cancelled=set(),
                )
            child_turns = await watcher.finish()
            if record.conversation_id:
                record.title = await self._title(record.conversation_id)
                response = await self.http.get(f"/conversations/{record.conversation_id}/tasks")
                record.tasks = [(t.status, t.content) for t in read(response, _TASKS)]
        except (httpx.HTTPError, RuntimeError, ValidationError) as exc:
            record.problem = f"{type(exc).__name__}: {exc}"
            child_turns = await watcher.finish()
        finally:
            # What the sample seeded or the agent saved would leak into later samples. Each
            # step on its own, so one failing leaves the rest done and the problem recorded.
            try:
                if memories_before is not None:
                    for memory_id in await self.memory_ids() - memories_before:
                        _ = await self.http.delete(f"/memory/{memory_id}")
                if events_before is not None:
                    for event_id in await self._event_ids() - events_before:
                        _ = await self.http.delete(f"/calendar/events/{event_id}")
            except httpx.HTTPError as exc:
                record.problem = record.problem or f"cleanup failed: {exc}"
            if scenario.routing:
                await context.router.reset()
        for agent in record.subagents:
            agent.turns = child_turns.get(agent.conversation_id, [])
        sandbox = (
            self.instance.data_dir / "sandbox" / "work" / safe_key(record.conversation_id)
            if record.conversation_id
            else None
        )
        evidence = Evidence(
            turns=record.turns,
            later=record.later,
            subagents=record.subagents,
            tasks=record.tasks,
            title=record.title,
            sandbox=sandbox,
            project=copy.path if copy else None,
            fixture=FIXTURES_DIR / scenario.fixture if scenario.fixture else None,
            cancelled=frozenset(cancelled),
            refs=context.tokens,
        )
        return record, evidence


def score(
    scenario: Scenario, record: Sample, evidence: Evidence, requests: list[ProxiedRequest]
) -> None:
    """Fill in the sample's requests and checks once the proxy's log for it is final."""
    record.requests = requests
    record.checks = evaluate(scenario.checks, replace(evidence, requests=requests))
