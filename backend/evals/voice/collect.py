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
Each model's pass rebinds ``main`` and ``utility`` to it, so the voice under test also
writes the titles and summaries; the original bindings are restored at the end. Output
lands in ``evals/voice/runs/<label>/``: ``A/``, ``B/``… each with ``transcript.md`` and
``run.json``, ``key.json`` naming the model behind each letter, and ``prompt.md``, the
Talos prompt the run used.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import string
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
from pydantic import BaseModel

from devkit.instance import resolve
from devkit.prompts import render as render_prompt
from evals.drive.client import DevClient, Policy
from evals.drive.instance import (
    HOSTED,
    Binding,
    Instance,
    NoMainBinding,
    Session,
    prompt_fingerprint,
    read,
    session,
    warm,
)
from evals.drive.keys import ENV_FILE, provider_key

from .cost import COST_FILE, render_cost
from .document import render
from .profile import Applied, available, load_profile
from .profile import apply as apply_profile
from .profile import remove as remove_profile
from .record import BARE, CONTEXT_DIR, RUN_FILE, Run, Sample, Turn, redact_home
from .spec import FIXTURES_DIR, KEY_FILE, PROJECT_TOKEN, RUNS_DIR, Scenario, load_scenarios

__all__ = ["HOSTED", "Collector", "main"]

_POLL_S = 15.0
#: The usage bucket for calls made after the scenarios.
_HELPERS = "helpers"
#: Prompt tokens per minute a hosted run stays under: 80% of a 2M limit, leaving room
#: for the requests already in flight when the window fills.
_DEFAULT_TPM = 1_600_000


class _Conversation(BaseModel):
    title: str | None = None
    work_summary: str | None = None


class Collector(Instance):
    """One model's pass over the scenarios, against one dev instance."""

    async def _turn(
        self, scenario: Scenario, prompt: str, conversation_id: str | None, project_id: str | None
    ) -> tuple[Turn, str]:
        run_id, conversation_id = await self.client.send(
            prompt,
            conversation_id,
            permission_level=scenario.permission_level,
            project_id=project_id if conversation_id is None else None,
        )
        policy = Policy(approve=scenario.approve == "all")
        turn = await self.follow(run_id, Turn(prompt=prompt), policy, timeout_s=scenario.timeout_s)
        return turn, conversation_id

    async def deliveries(self, scenario: Scenario, record: Sample, conversation_id: str) -> None:
        found = await self.wait_deliveries(
            conversation_id,
            Policy(approve=scenario.approve == "all"),
            timeout_s=scenario.timeout_s,
            on_turn=record.turns.append,
        )
        record.subagents = found.subagents
        if found.problem:
            record.problem = found.problem

    async def sample(self, scenario: Scenario, number: int) -> Sample:
        record = Sample(scenario=scenario.id, sample=number)
        before = await self.memory_ids()
        try:
            for memory in scenario.memories:
                _ = (await self.http.post("/memory", json=memory.model_dump())).raise_for_status()
            project_id, project_path = (None, "")
            if scenario.fixture:
                copy = await self.fixture(FIXTURES_DIR / scenario.fixture, "voice-fixtures")
                project_id, project_path = copy.project_id, copy.home_path
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

    async def helpers(self, samples: list[Sample], wait_s: float) -> None:
        """Fill in titles at once and work summaries as the idle sweep writes them.

        A live title races the turn for the endpoint and times out on a server that runs
        one request at a time, so a missing one is regenerated through the retitle route."""
        deadline = time.monotonic() + wait_s
        pending = [s for s in samples if s.conversation_id]
        for sample in pending:
            response = await self.http.get(f"/conversations/{sample.conversation_id}")
            if read(response, _Conversation).title is None:
                retitled = await self.http.post(f"/conversations/{sample.conversation_id}/retitle")
                sample.title_regenerated = retitled.is_success
        while True:
            waiting: list[Sample] = []
            for sample in pending:
                response = await self.http.get(f"/conversations/{sample.conversation_id}")
                found = read(response, _Conversation)
                sample.title = found.title
                sample.work_summary = found.work_summary
                if found.work_summary is None:
                    waiting.append(sample)
            pending = waiting
            if not pending or time.monotonic() > deadline:
                return
            await asyncio.sleep(_POLL_S)


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
    live: Session,
) -> None:
    """Every sample of every scenario, saved after each so a crash keeps what ran."""
    for scenario in scenarios:
        for number in range(1, samples + 1):
            print(f"  {scenario.id} #{number} …", flush=True)
            if number == 1:
                live.proxy.arm(run_dir / CONTEXT_DIR / f"{scenario.id}.json")
            bucket = f"{scenario.id}#{number}"
            live.attribute(bucket)
            record = await collector.sample(scenario, number)
            record.usage = live.take(bucket)
            run.samples.append(record)
            _save(run, run_dir, scenarios)
            print(f"    {record.problem or 'ok'}", flush=True)
    print("  waiting for titles and work summaries …", flush=True)
    live.attribute(_HELPERS)
    await collector.helpers(run.samples, summary_wait)
    run.helper_usage = live.take(_HELPERS)
    run.peak_tokens_per_minute = live.window.peak
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
    applied = Applied()
    tpm = args.tpm or (_DEFAULT_TPM if hosted else None)
    try:
        async with session(
            collector, hosted=hosted, api_key=api_key, tokens_per_minute=tpm
        ) as live:
            group_dir.mkdir(parents=True)
            _ = (group_dir / "prompt.md").write_text(f"`{fingerprint}`\n\n{prompt}\n")
            _ = (group_dir / KEY_FILE).write_text(json.dumps(letters, indent=2) + "\n")
            try:
                if profile:
                    print(f"Applying the {profile.name} profile …", flush=True)
                    await apply_profile(collector.http, instance.data_dir, profile, applied)
                for letter, model in letters.items():
                    print(f"→ run {letter}", flush=True)
                    binding = Binding(endpoint_ids=[live.endpoint.id], model=model)
                    await collector.bind("main", binding)
                    await collector.bind("utility", binding)
                    if not live.stub and not live.temporary:
                        await warm(live.endpoint.base_url, model)
                    run = Run(
                        group=label,
                        name=letter,
                        started_at=f"{datetime.now(UTC):%Y-%m-%d %H:%M} UTC",
                        prompt_fingerprint=fingerprint,
                        stub=live.stub,
                        profile=profile_name,
                        provider=live.endpoint.provider,
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
                        live=live,
                    )
                    print(f"  wrote {run_dir / 'transcript.md'}", flush=True)
            finally:
                for problem in await remove_profile(collector.http, applied):
                    print(f"Could not undo: {problem}")
    except NoMainBinding as exc:
        print(exc)
        return 2
    finally:
        await client.aclose()
    print(f"\nRun group: {group_dir}\nGrade each transcript by evals/voice/GRADING.md.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
