"""Run the harness scenarios against the dev instance and write one transcript per model.

    uv run python -m evals.harness.run [--models M ...] [--only ID ...] [--area AREA ...]
        [--label NAME] [--samples N] [--provider anthropic] [--tpm N]
        [--local ENDPOINT/MODEL] [--hosted ENDPOINT/MODEL]
        [--subagent-model ENDPOINT/MODEL] [--choosable ENDPOINT/MODEL[=NOTE] ...]

The dev instance must be up (``dev_instance.py up --detach --with-vm``). Without
``--models`` the run is one pass, ``A``, on the current ``main`` binding; with them, each
model gets a pass with ``main`` and ``utility`` bound to it. ``--provider`` adds a
temporary hosted endpoint, keyed from the environment or ``evals/voice/.env``.

``--local`` and ``--hosted`` name the two models the routing scenarios route between;
``--hosted`` defaults to the model under test. On the dev stub, a second stub endpoint,
``stub-local``, is added for the run and ``--local`` defaults to it. ``--subagent-model``
binds the ``subagent`` role and ``--choosable`` sets the choosable list for every
scenario that doesn't set its own; everything is restored at the end.

On the stub only the scenarios that carry ``stub`` replies run. Output lands in
``evals/harness/runs/<label>/<letter>/``: ``run.json``, ``transcript.md``, ``cost.md`` and
``context/``, with ``key.json`` in the group. Grade with ``.claude/workflows/eval-grade.js``
(``suite: "harness"``), then file the issues with ``python -m evals.harness.issues``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import string
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx

from devkit.instance import DevInstance, resolve
from devkit.prompts import render as render_prompt
from evals.drive.client import DevClient
from evals.drive.cost import COST_FILE, render_cost
from evals.drive.instance import (
    HOSTED,
    Binding,
    Endpoint,
    NoMainBinding,
    Session,
    prompt_fingerprint,
    read,
    session,
    warm,
)
from evals.drive.keys import ENV_FILE, provider_key
from evals.drive.timeline import redact_home

from .checks import ProxiedRequest
from .document import render
from .record import CONTEXT_DIR, RUN_FILE, TRANSCRIPT_FILE, Routing, Run
from .routing import RefError, Router, split_ref
from .sample import Context, Harness, score
from .spec import HOSTED_TOKEN, KEY_FILE, LOCAL_TOKEN, RUNS_DIR, Choice, Scenario, load_scenarios

#: The stub endpoint a stub run adds so routing scenarios have a second model to pick.
STUB_LOCAL = "stub-local"
_DEFAULT_TPM = 1_600_000


class _Args(argparse.Namespace):
    models: list[str] | None = None
    only: list[str] | None = None
    area: list[str] | None = None
    label: str | None = None
    samples: int = 1
    provider: str | None = None
    tpm: int | None = None
    local: str | None = None
    hosted: str | None = None
    subagent_model: str = ""
    choosable: list[str] | None = None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="evals.harness.run")
    add = parser.add_argument
    _ = add("--models", nargs="+", help="model ids on the main endpoint, one pass each")
    _ = add("--only", nargs="*", help="scenario ids to run")
    _ = add("--area", nargs="*", help="scenario areas to run")
    _ = add("--label", help="name of the run group")
    _ = add("--samples", type=int, default=1)
    _ = add(
        "--provider", choices=sorted(HOSTED), help=f"a temporary endpoint keyed from {ENV_FILE}"
    )
    _ = add("--tpm", type=int, help="prompt tokens per minute to stay under")
    _ = add("--local", help="the routing scenarios' local model, ENDPOINT/MODEL")
    _ = add("--hosted", help="the routing scenarios' hosted model (default: the model under test)")
    _ = add("--subagent-model", default="", help="bind the subagent role, ENDPOINT/MODEL")
    _ = add("--choosable", nargs="*", help="the choosable list, ENDPOINT/MODEL[=NOTE] each")
    return parser


def _choices(raw: list[str] | None) -> list[Choice]:
    out: list[Choice] = []
    for item in raw or []:
        ref, _, note = item.partition("=")
        _ = split_ref(ref)
        out.append(Choice(ref=ref, note=note))
    return out


def _select(scenarios: list[Scenario], args: _Args) -> list[Scenario] | str:
    if args.only:
        unknown = sorted(set(args.only) - {s.id for s in scenarios})
        if unknown:
            return f"Unknown scenario ids: {', '.join(unknown)}"
        scenarios = [s for s in scenarios if s.id in args.only]
    if args.area:
        scenarios = [s for s in scenarios if s.area in args.area]
    return scenarios


def _skip(scenario: Scenario, instance: DevInstance, stub: bool, router: Router) -> str | None:
    """Why the scenario can't run on this instance, or None."""
    if stub and not scenario.stub:
        return "no stub script"
    if scenario.requires and not instance.vm:
        return f"needs {' and '.join(scenario.requires)} (start with --with-vm)"
    return router.resolvable(scenario.routing)


def _save(run: Run, run_dir: Path, scenarios: list[Scenario]) -> None:
    _ = (run_dir / RUN_FILE).write_text(redact_home(run.model_dump_json(indent=2)))
    _ = (run_dir / TRANSCRIPT_FILE).write_text(redact_home(render(run, scenarios)))


async def _stub_local(driver: Harness, main: Endpoint) -> Endpoint:
    """The second stub endpoint, reusing one an interrupted run left behind."""
    for endpoint in await driver.endpoints():
        if endpoint.name == STUB_LOCAL:
            return endpoint
    body = {
        "name": STUB_LOCAL,
        "provider": main.provider,
        "base_url": main.base_url,
        "model": main.model,
        "context_window": 128_000,
    }
    return read(await driver.http.post("/models/endpoints", json=body), Endpoint)


async def _pass(
    driver: Harness,
    run: Run,
    run_dir: Path,
    scenarios: list[Scenario],
    *,
    samples: int,
    live: Session,
    context: Context,
) -> None:
    for scenario in scenarios:
        if reason := _skip(scenario, driver.instance, live.stub, context.router):
            run.skipped[scenario.id] = reason
            continue
        for number in range(1, samples + 1):
            print(f"  {scenario.id} #{number} …", flush=True)
            if number == 1:
                live.proxy.arm(run_dir / CONTEXT_DIR / f"{scenario.id}.json")
            bucket = f"{scenario.id}#{number}"
            live.attribute(bucket)
            record, evidence = await driver.sample(scenario, number, context)
            record.usage = live.take(bucket)
            requests = [
                ProxiedRequest(endpoint=r.endpoint, model=r.model, tools=r.tools)
                for r in live.requests(bucket)
            ]
            score(scenario, record, evidence, requests)
            run.samples.append(record)
            _save(run, run_dir, scenarios)
            failed = [c.label for c in record.failed_checks()]
            print(f"    {record.problem or 'ok'}; {len(failed)} checks failed", flush=True)
            for label in failed:
                print(f"      - {label}", flush=True)
    run.peak_tokens_per_minute = live.window.peak
    _save(run, run_dir, scenarios)
    _ = (run_dir / COST_FILE).write_text(render_cost(run))


async def main(argv: list[str]) -> int:
    args = _parser().parse_args(argv, namespace=_Args())
    hosted = HOSTED[args.provider] if args.provider else None
    api_key = provider_key(hosted.key) if hosted else None
    if hosted and not api_key:
        print(f"No {hosted.key}: set it in the environment or in {ENV_FILE}")
        return 2
    try:
        choosable = _choices(args.choosable)
    except RefError as exc:
        print(exc)
        return 2
    instance = resolve()
    selected = _select(load_scenarios(), args)
    if isinstance(selected, str):
        print(selected)
        return 2
    label = args.label or f"{datetime.now(UTC):%Y-%m-%d-%H%M}-harness"
    group_dir = RUNS_DIR / label
    if group_dir.exists():
        print(f"{group_dir} already exists; pick another --label")
        return 2
    prompt = render_prompt("talos")
    fingerprint = prompt_fingerprint(prompt)
    if hosted and not args.models:
        print("--provider needs --models: the temporary endpoint has no model of its own.")
        return 2
    models: list[str | None] = list(args.models) if args.models else [None]
    random.shuffle(models)
    letters = dict(zip(string.ascii_uppercase, models, strict=False))

    client = DevClient(instance.backend_url)
    driver = Harness(client, instance)
    added: Endpoint | None = None
    router: Router | None = None
    tpm = args.tpm or (_DEFAULT_TPM if hosted else None)
    try:
        original = await driver.bindings()
        main_binding = original.get("main")
        if not hosted and (main_binding is None or not main_binding.endpoint_ids):
            print("The instance has no main binding to run on.")
            return 2
        if main_binding and main_binding.endpoint_ids and not hosted:
            head = await driver.endpoint(main_binding.endpoint_ids[0])
            if urlparse(head.base_url).port == instance.stub_port and not args.local:
                added = await _stub_local(driver, head)
        endpoints = {e.name: e for e in await driver.endpoints()}
        router = Router(driver, endpoints, choosable=choosable, subagent=args.subagent_model)
        referenced = [args.local or "", args.subagent_model, *(c.ref for c in choosable)]
        also = [added.id] if added else []
        for ref in filter(None, referenced):
            try:
                also.append(router.endpoint_id(ref)[0])
            except RefError as exc:
                print(exc)
                return 2
        await router.save()
        async with session(
            driver,
            hosted=hosted,
            api_key=api_key,
            tokens_per_minute=tpm,
            label="harness-eval",
            also=also,
            restore=("main", "utility", "subagent"),
        ) as live:
            router.endpoints = {e.name: e for e in await driver.endpoints()}
            group_dir.mkdir(parents=True)
            _ = (group_dir / "prompt.md").write_text(f"`{fingerprint}`\n\n{prompt}\n")
            _ = (group_dir / KEY_FILE).write_text(json.dumps(letters, indent=2) + "\n")
            for letter, model in letters.items():
                print(f"→ run {letter}", flush=True)
                if model is not None:
                    binding = Binding(endpoint_ids=[live.endpoint.id], model=model)
                    await driver.bind("main", binding)
                    await driver.bind("utility", binding)
                    if not live.stub and not live.temporary:
                        await warm(live.endpoint.base_url, model)
                under_test = model or (main_binding.model if main_binding else None)
                under_test = under_test or live.endpoint.model or ""
                local = args.local or (f"{STUB_LOCAL}/{under_test}" if added else "")
                hosted_ref = args.hosted or f"{live.endpoint.name}/{under_test}"
                router.tokens = {LOCAL_TOKEN: local, HOSTED_TOKEN: hosted_ref}
                await router.run_defaults()
                stub_url = f"http://127.0.0.1:{instance.stub_port}" if live.stub else None
                run = Run(
                    group=label,
                    name=letter,
                    started_at=f"{datetime.now(UTC):%Y-%m-%d %H:%M} UTC",
                    prompt_fingerprint=fingerprint,
                    stub=live.stub,
                    provider=live.endpoint.provider,
                    routing=Routing(
                        local=local,
                        hosted=hosted_ref,
                        subagent=args.subagent_model,
                        choosable=[c.ref for c in choosable],
                    ),
                )
                run_dir = group_dir / letter
                run_dir.mkdir()
                await _pass(
                    driver,
                    run,
                    run_dir,
                    selected,
                    samples=args.samples,
                    live=live,
                    context=Context(router, stub_url, dict(router.tokens)),
                )
                print(f"  wrote {run_dir / TRANSCRIPT_FILE}", flush=True)
    except NoMainBinding as exc:
        print(exc)
        return 2
    finally:
        for problem in await router.restore() if router else []:
            print(f"Could not restore: {problem}")
        if added is not None:
            try:
                await driver.remove_endpoint(added.id)
            except httpx.HTTPError as exc:
                print(f"Could not remove {STUB_LOCAL}: {exc}")
        await client.aclose()
    print(f'\nRun group: {group_dir}\nGrade it with eval-grade.js, suite "harness".')
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
