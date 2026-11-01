"""Run the voice scenarios on each model and write one blind transcript per model.

    uv run python -m evals.voice.collect --models M [M ...] [--only ID ...] [--label NAME]
        [--samples N] [--summary-wait S] [--profile NAME] [--provider anthropic] [--tpm N]

The dev instance must be up (``dev_instance.py up --detach --with-vm``) with its ``main``
role bound to the endpoint serving the models, or ``--provider`` given: the run then adds
a temporary endpoint keyed from the environment or ``evals/voice/.env`` and deletes it at
the end. For Anthropic runs, start the instance with ``HEPHAESTUS_ANTHROPIC_CACHE_TTL=5m``:
the eval sends requests back to back, so the one-hour cache chat uses only costs more to
write. Each run's ``cost.md`` breaks down its token use and cost, and ``--tpm`` caps the
prompt tokens sent per minute. The label defaults to the UTC start time and the profile.
Each model's pass
rebinds ``main`` and ``utility`` to it, so the voice under test also writes the titles and
summaries; the original bindings are restored at the end. Output lands in
``evals/voice/runs/<label>/``: ``A/``, ``B/``… each with ``transcript.md`` and ``run.json``,
``key.json`` naming the model behind each letter, and ``prompt.md``, the Talos prompt the
run used.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import random
import secrets
import shutil
import string
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field, TypeAdapter

from devkit.instance import DevInstance, resolve
from devkit.prompts import render as render_prompt
from evals.tasks.client import DevClient, Settler
from runs.events.approvals import ApprovalRequired, QuestionAsked

from .cost import COST_FILE, render_cost
from .document import render
from .keys import ENV_FILE, provider_key
from .profile import Applied, available, load_profile
from .profile import apply as apply_profile
from .profile import remove as remove_profile
from .proxy import RecordingProxy
from .record import (
    BARE,
    CONTEXT_DIR,
    RUN_FILE,
    Run,
    Sample,
    SubagentRecord,
    Timeline,
    Turn,
    redact_home,
)
from .spec import FIXTURES_DIR, KEY_FILE, PROJECT_TOKEN, RUNS_DIR, Scenario, load_scenarios

_POLL_S = 15.0
_DELIVERY_POLL_S = 5.0
#: The usage bucket for calls made after the scenarios.
_HELPERS = "helpers"
#: Prompt tokens per minute a hosted run stays under: 80% of a 2M limit, leaving room
#: for the requests already in flight when the window fills.
_DEFAULT_TPM = 1_600_000

#: The run kind the inbox starts to hand a thread its sub-agents' reports.
_WAKE_KIND = "wake"
_LIVE = frozenset({"running", "blocked"})


class _Binding(BaseModel):
    endpoint_ids: list[str]
    model: str | None = None


class _Endpoint(BaseModel):
    id: str
    base_url: str
    provider: str


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


class _Conversation(BaseModel):
    title: str | None = None
    work_summary: str | None = None


class _Card(BaseModel):
    handle: str
    callsign: str = ""
    agent_class: str = ""
    task: str
    status: str
    summary: str | None = None
    findings: object = None
    error: str | None = None


class _Cards(BaseModel):
    subagents: list[_Card]


class _ChatSettings(BaseModel):
    work_summary_idle_minutes: int


class _RunView(BaseModel):
    id: str
    kind: str
    created_at: datetime
    conversation_id: str | None = Field(default=None, alias="conversationId")


_BINDINGS: TypeAdapter[dict[str, _Binding]] = TypeAdapter(dict[str, _Binding])
_RUNS: TypeAdapter[list[_RunView]] = TypeAdapter(list[_RunView])
_MEMORIES: TypeAdapter[list[_Memory]] = TypeAdapter(list[_Memory])


def prompt_fingerprint(prompt: str) -> str:
    return hashlib.sha256(prompt.encode()).hexdigest()[:12]


def _read[T](response: httpx.Response, shape: type[T] | TypeAdapter[T]) -> T:
    _ = response.raise_for_status()
    adapter = shape if isinstance(shape, TypeAdapter) else TypeAdapter(shape)
    return adapter.validate_json(response.content)


class Collector:
    """One model's pass over the scenarios, against one dev instance."""

    def __init__(self, client: DevClient, instance: DevInstance) -> None:
        self.client: DevClient = client
        self.http: httpx.AsyncClient = client.http
        self.instance: DevInstance = instance

    async def bindings(self) -> dict[str, _Binding]:
        return _read(await self.http.get("/models/roles"), _BINDINGS)

    async def bind(self, role: str, binding: _Binding) -> None:
        response = await self.http.put(f"/models/roles/{role}", json=binding.model_dump())
        _ = response.raise_for_status()

    async def endpoint(self, endpoint_id: str) -> _Endpoint:
        return _read(await self.http.get(f"/models/endpoints/{endpoint_id}"), _Endpoint)

    async def add_endpoint(self, hosted: Hosted, api_key: str) -> _Endpoint:
        body = {
            "name": f"voice-eval {hosted.provider}",
            "provider": hosted.provider,
            "base_url": hosted.base_url,
            "api_key": api_key,
            "vision": True,
        }
        return _read(await self.http.post("/models/endpoints", json=body), _Endpoint)

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
        return _read(response, _ChatSettings).work_summary_idle_minutes

    async def memory_ids(self) -> set[str]:
        return {memory.id for memory in _read(await self.http.get("/memory"), _MEMORIES)}

    async def _fixture(self, name: str) -> tuple[str, str]:
        """A fresh copy of a fixture, registered as a project: ``(project_id, home path)``.

        The copy's directory name is already a valid slug and unique, so the project's
        ``~/projects/<slug>`` is that name."""
        slug = f"{name}-{secrets.token_hex(3)}"
        target = self.instance.data_dir / "voice-fixtures" / slug
        _ = shutil.copytree(FIXTURES_DIR / name, target)
        response = await self.http.post("/projects/ensure", json={"root_path": str(target)})
        return _read(response, _Project).id, f"~/projects/{slug}"

    async def _turn(
        self, scenario: Scenario, prompt: str, conversation_id: str | None, project_id: str | None
    ) -> tuple[Turn, str]:
        run_id, conversation_id = await self.client.send(
            prompt,
            conversation_id,
            permission_level=scenario.permission_level,
            project_id=project_id if conversation_id is None else None,
        )
        return await self._follow(scenario, run_id, Turn(prompt=prompt)), conversation_id

    async def _follow(self, scenario: Scenario, run_id: str, turn: Turn) -> Turn:
        """Record one run into ``turn``, answering what it parks on, until it ends."""
        approve = scenario.approve == "all"
        timeline = Timeline(turn, approve=approve)
        settler = Settler(self.client, run_id, approve=approve)
        started = time.monotonic()

        async def follow() -> None:
            async for frame in self.client.frames(run_id):
                timeline.feed(frame.body)
                if isinstance(frame.body, ApprovalRequired | QuestionAsked):
                    settler.park(frame.body)

        try:
            await asyncio.wait_for(follow(), timeout=scenario.timeout_s)
        except TimeoutError:
            turn.detail = f"no terminal event after {scenario.timeout_s:.0f}s"
        turn.wall_s = round(time.monotonic() - started, 1)
        return turn

    async def deliveries(self, scenario: Scenario, record: Sample, conversation_id: str) -> None:
        """Wait out the thread's sub-agents, recording each run that delivers a report.

        A report reaches the parent as a separate ``wake`` run the inbox starts once the
        child ends, so the reply that relays it is only seen by following those runs."""
        deadline = time.monotonic() + scenario.timeout_s
        followed: set[str] = set()
        while True:
            response = await self.http.get("/runs", params={"active": "false"})
            wakes = sorted(
                (
                    run
                    for run in _read(response, _RUNS)
                    if run.kind == _WAKE_KIND
                    and run.conversation_id == conversation_id
                    and run.id not in followed
                ),
                key=lambda run: run.created_at,
            )
            for run in wakes:
                followed.add(run.id)
                turn = await self._follow(scenario, run.id, Turn(prompt="", delivery=True))
                record.turns.append(turn)
            await self._subagents(record, conversation_id)
            live = [a for a in record.subagents if a.status in _LIVE]
            if not live and not wakes:
                return
            if time.monotonic() > deadline:
                record.problem = f"sub-agents still live after {scenario.timeout_s:.0f}s"
                return
            await asyncio.sleep(_DELIVERY_POLL_S)

    async def sample(self, scenario: Scenario, number: int) -> Sample:
        record = Sample(scenario=scenario.id, sample=number)
        before = await self.memory_ids()
        try:
            for memory in scenario.memories:
                _ = (await self.http.post("/memory", json=memory.model_dump())).raise_for_status()
            project_id, project_path = (None, "")
            if scenario.fixture:
                project_id, project_path = await self._fixture(scenario.fixture)
            for prompt in scenario.prompts:
                text = prompt.replace(PROJECT_TOKEN, project_path)
                turn, record.conversation_id = await self._turn(
                    scenario, text, record.conversation_id, project_id
                )
                record.turns.append(turn)
                if turn.ended != "done":
                    record.problem = f"turn {len(record.turns)} ended {turn.ended}"
                    break
            if record.conversation_id:
                await self.deliveries(scenario, record, record.conversation_id)
        except httpx.HTTPError as exc:
            record.problem = f"{type(exc).__name__}: {exc}"
        finally:
            # Memories the scenario seeded or the model saved would leak into later samples.
            for memory_id in await self.memory_ids() - before:
                _ = await self.http.delete(f"/memory/{memory_id}")
        return record

    async def _subagents(self, record: Sample, conversation_id: str) -> None:
        cards = _read(await self.http.get(f"/conversations/{conversation_id}/subagents"), _Cards)
        record.subagents = [
            SubagentRecord(
                handle=card.handle,
                callsign=card.callsign,
                agent_class=card.agent_class,
                task=card.task,
                status=card.status,
                report=card.summary,
                findings=card.findings,
                error=card.error,
            )
            for card in cards.subagents
        ]

    async def helpers(self, samples: list[Sample], wait_s: float) -> None:
        """Fill in titles at once and work summaries as the idle sweep writes them.

        A live title races the turn for the endpoint and times out on a server that runs
        one request at a time, so a missing one is regenerated through the retitle route."""
        deadline = time.monotonic() + wait_s
        pending = [s for s in samples if s.conversation_id]
        for sample in pending:
            response = await self.http.get(f"/conversations/{sample.conversation_id}")
            if _read(response, _Conversation).title is None:
                retitled = await self.http.post(f"/conversations/{sample.conversation_id}/retitle")
                sample.title_regenerated = retitled.is_success
        while True:
            waiting: list[Sample] = []
            for sample in pending:
                response = await self.http.get(f"/conversations/{sample.conversation_id}")
                found = _read(response, _Conversation)
                sample.title = found.title
                sample.work_summary = found.work_summary
                if found.work_summary is None:
                    waiting.append(sample)
            pending = waiting
            if not pending or time.monotonic() > deadline:
                return
            await asyncio.sleep(_POLL_S)


async def _warm(base_url: str, model: str) -> None:
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


class _Args(argparse.Namespace):
    models: list[str] = []
    only: list[str] | None = None
    label: str | None = None
    samples: int = 2
    summary_wait: float = 600.0
    profile: str | None = None
    provider: str | None = None
    tpm: int | None = None


def _save(run: Run, run_dir: Path, scenarios: list[Scenario]) -> None:
    _ = (run_dir / RUN_FILE).write_text(redact_home(run.model_dump_json(indent=2)))
    _ = (run_dir / "transcript.md").write_text(redact_home(render(run, scenarios)))


async def _pass(
    collector: Collector,
    run: Run,
    run_dir: Path,
    scenarios: list[Scenario],
    *,
    samples: int,
    summary_wait: float,
    proxy: RecordingProxy,
) -> None:
    """Every sample of every scenario, saved after each so a crash keeps what ran."""
    for scenario in scenarios:
        for number in range(1, samples + 1):
            print(f"  {scenario.id} #{number} …", flush=True)
            if number == 1:
                proxy.arm(run_dir / CONTEXT_DIR / f"{scenario.id}.json")
            bucket = f"{scenario.id}#{number}"
            proxy.attribute(bucket)
            record = await collector.sample(scenario, number)
            record.usage = proxy.take(bucket)
            run.samples.append(record)
            _save(run, run_dir, scenarios)
            print(f"    {record.problem or 'ok'}", flush=True)
    print("  waiting for titles and work summaries …", flush=True)
    proxy.attribute(_HELPERS)
    await collector.helpers(run.samples, summary_wait)
    run.helper_usage = proxy.take(_HELPERS)
    run.peak_tokens_per_minute = proxy.window.peak
    _save(run, run_dir, scenarios)
    _ = (run_dir / COST_FILE).write_text(render_cost(run))
    spent = run.usage()
    if spent.requests:
        print(
            f"  cost ${spent.cost_usd:.2f} over {spent.requests} requests, "
            + f"{spent.cache_read_share:.0%} of prompt tokens from cache",
            flush=True,
        )


async def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="evals.voice.collect")
    _ = parser.add_argument(
        "--models", nargs="+", required=True, help="model ids on the main endpoint"
    )
    _ = parser.add_argument("--only", nargs="*", help="scenario ids to run")
    _ = parser.add_argument("--label", help="name of the run group")
    _ = parser.add_argument("--samples", type=int, default=2)
    _ = parser.add_argument("--summary-wait", type=float, default=600.0)
    _ = parser.add_argument(
        "--profile", help=f"workspace profile to run in: {', '.join(available())}"
    )
    _ = parser.add_argument(
        "--provider",
        choices=sorted(HOSTED),
        help=f"run on a temporary endpoint for this provider, keyed from env or {ENV_FILE}",
    )
    _ = parser.add_argument(
        "--tpm",
        type=int,
        help=f"prompt tokens per minute to stay under (default {_DEFAULT_TPM:,} "
        + "with --provider, none otherwise)",
    )
    args = parser.parse_args(argv, namespace=_Args())
    try:
        profile = load_profile(args.profile) if args.profile else None
    except ValueError as exc:
        print(exc)
        return 2
    hosted = HOSTED[args.provider] if args.provider else None
    api_key = provider_key(hosted.key) if hosted else None
    if hosted and not api_key:
        print(f"No {hosted.key}: set it in the environment or in {ENV_FILE}")
        return 2

    instance = resolve()
    scenarios = load_scenarios()
    if args.only:
        unknown = sorted(set(args.only) - {s.id for s in scenarios})
        if unknown:
            print(f"Unknown scenario ids: {', '.join(unknown)}")
            return 2
        scenarios = [s for s in scenarios if s.id in args.only]
    if not instance.vm:
        skipped = [s.id for s in scenarios if s.requires_vm]
        scenarios = [s for s in scenarios if not s.requires_vm]
        if skipped:
            print(f"No VM on this instance; skipping {', '.join(skipped)}")

    profile_name = profile.name if profile else BARE
    label = args.label or f"{datetime.now(UTC):%Y-%m-%d-%H%M}-{profile_name}"
    group_dir = RUNS_DIR / label
    if group_dir.exists():
        print(f"{group_dir} already exists; pick another --label")
        return 2
    prompt = render_prompt("talos")
    fingerprint = prompt_fingerprint(prompt)
    models = list(args.models)
    random.shuffle(models)
    letters = dict(zip(string.ascii_uppercase, models, strict=False))

    client = DevClient(instance.backend_url)
    collector = Collector(client, instance)
    original = await collector.bindings()
    if not hosted and ("main" not in original or not original["main"].endpoint_ids):
        print("The instance has no main binding to run on.")
        return 2
    idle_before = await collector.idle_minutes()
    group_dir.mkdir(parents=True)
    _ = (group_dir / "prompt.md").write_text(f"`{fingerprint}`\n\n{prompt}\n")
    _ = (group_dir / KEY_FILE).write_text(json.dumps(letters, indent=2) + "\n")
    applied = Applied()
    if hosted and api_key:
        endpoint = await collector.add_endpoint(hosted, api_key)
        temporary = True
    else:
        endpoint = await collector.endpoint(original["main"].endpoint_ids[0])
        temporary = False
    stub = urlparse(endpoint.base_url).port == instance.stub_port
    tpm = args.tpm or (_DEFAULT_TPM if hosted else None)
    proxy = RecordingProxy(endpoint.base_url, tokens_per_minute=tpm)
    try:
        await proxy.start()
        await collector.point(endpoint.id, proxy.base_url)
        if profile:
            print(f"Applying the {profile.name} profile …", flush=True)
            await apply_profile(collector.http, instance.data_dir, profile, applied)
        _ = await collector.idle_minutes(1)
        for letter, model in letters.items():
            print(f"→ run {letter}", flush=True)
            binding = _Binding(endpoint_ids=[endpoint.id], model=model)
            await collector.bind("main", binding)
            await collector.bind("utility", binding)
            if not stub and not temporary:
                await _warm(endpoint.base_url, model)
            run = Run(
                group=label,
                name=letter,
                started_at=f"{datetime.now(UTC):%Y-%m-%d %H:%M} UTC",
                prompt_fingerprint=fingerprint,
                stub=stub,
                profile=profile_name,
                provider=endpoint.provider,
            )
            run_dir = group_dir / letter
            run_dir.mkdir()
            await _pass(
                collector,
                run,
                run_dir,
                scenarios,
                samples=args.samples,
                summary_wait=args.summary_wait,
                proxy=proxy,
            )
            print(f"  wrote {run_dir / 'transcript.md'}", flush=True)
    finally:
        for problem in await remove_profile(collector.http, applied):
            print(f"Could not undo: {problem}")
        if not temporary:
            await collector.point(endpoint.id, endpoint.base_url)
        await proxy.stop()
        for role in ("main", "utility"):
            if role in original:
                await collector.bind(role, original[role])
        if temporary:
            await collector.remove_endpoint(endpoint.id)
        _ = await collector.idle_minutes(idle_before)
        await client.aclose()
    print(f"\nRun group: {group_dir}\nGrade each transcript by evals/voice/GRADING.md.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
