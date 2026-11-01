"""Run the task suite against the dev instance and record how each task ended.

    uv run python -m evals.tasks.runner [--only ID ...] [--label NAME] [--no-judge]

The dev instance must be up with ``HEPH_DEV_CHAT_*`` naming the model under test
(``dev_instance.py up --detach --with-vm``). Each task opens a new thread at the default
permission level, answers whatever it parks on per the task's policy, and is scored on
its checks once its last turn ends. Results land in ``evals/tasks/results/``.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from devkit import live
from devkit.instance import resolve
from evals.tasks.client import DevClient, Settler
from evals.tasks.judge import judge
from evals.tasks.report import write_summary
from evals.tasks.results import Outcome, TaskResult, TurnRecord
from evals.tasks.spec import Evidence, Task, load_tasks
from runs.events.approvals import ApprovalRequired, QuestionAsked
from runs.events.lifecycle import (
    AnswerDelta,
    LimitNotice,
    RunEnded,
    RunError,
    StepStarted,
    ThinkingDelta,
)
from runs.events.tools import ToolCompleted, ToolFailed, ToolStarted
from services.sandbox.base import safe_key

RESULTS_DIR = Path(__file__).with_name("results")


@dataclass
class _Turn:
    """What one turn showed on its stream, accumulated as the frames arrive."""

    record: TurnRecord
    answer: list[str] = field(default_factory=list)
    tools_completed: list[str] = field(default_factory=list)
    step_started_at: float | None = None


async def _run_turn(
    client: DevClient,
    task: Task,
    prompt: str,
    thread: str | None,
) -> tuple[_Turn, str]:
    run_id, conversation_id = await client.send(prompt, thread)
    turn = _Turn(record=TurnRecord(ended="timeout"))
    started = time.monotonic()
    settler = Settler(client, run_id, approve=task.approve == "all")

    async def follow() -> None:
        async for frame in client.frames(run_id):
            body = frame.body
            now = time.monotonic()
            if isinstance(body, StepStarted):
                turn.record.steps += 1
                turn.step_started_at = now
            elif isinstance(body, ThinkingDelta | AnswerDelta):
                if turn.step_started_at is not None:
                    turn.record.first_token_s.append(round(now - turn.step_started_at, 1))
                    turn.step_started_at = None
                if isinstance(body, AnswerDelta):
                    turn.answer.append(body.text)
            elif isinstance(body, ToolStarted):
                turn.record.tool_calls += 1
            elif isinstance(body, ToolCompleted):
                turn.tools_completed.append(body.name)
            elif isinstance(body, ToolFailed):
                turn.record.tool_failures.append(f"{body.name}: {body.error[:200]}")
            elif isinstance(body, LimitNotice):
                turn.record.limits.append(body.limit)
            elif isinstance(body, ApprovalRequired | QuestionAsked):
                if isinstance(body, ApprovalRequired):
                    turn.record.approvals += 1
                else:
                    turn.record.questions += 1
                settler.park(body)
            elif isinstance(body, RunEnded):
                turn.record.ended = body.outcome
                turn.record.detail = body.detail
            elif isinstance(body, RunError):
                turn.record.ended = "error"
                turn.record.detail = f"{body.kind}: {body.message}"

    try:
        await asyncio.wait_for(follow(), timeout=task.timeout_s)
    except TimeoutError:
        turn.record.detail = f"no terminal event after {task.timeout_s:.0f}s"
    turn.record.wall_s = round(time.monotonic() - started, 1)
    return turn, conversation_id


async def run_task(
    task: Task, *, base_url: str, data_dir: Path, vm: bool, use_judge: bool
) -> TaskResult:
    if task.requires and not vm:
        return TaskResult(id=task.id, area=task.area, outcome="skipped", reason="needs --with-vm")
    client = DevClient(base_url)
    turns: list[_Turn] = []
    conversation_id: str | None = None
    try:
        for prompt in task.prompts:
            turn, conversation_id = await _run_turn(client, task, prompt, conversation_id)
            turns.append(turn)
            if turn.record.ended != "done":
                break
    finally:
        await client.aclose()

    assert conversation_id is not None
    answer = "".join(turns[-1].answer)
    evidence = Evidence(
        answer=answer,
        tools_completed=[name for turn in turns for name in turn.tools_completed],
        workdir=data_dir / "sandbox" / "work" / safe_key(conversation_id),
    )
    checks = {check.label(): check.passes(evidence) for check in task.checks}
    score = await judge(task, answer) if use_judge and task.rubric else None

    finished = all(turn.record.ended == "done" for turn in turns) and len(turns) == len(
        task.prompts
    )
    passed = sum(checks.values()) + (1 if score == 2 else 0)
    wanted = len(checks) + (1 if score is not None else 0)
    if finished and passed == wanted:
        outcome: Outcome = "pass"
    elif passed > 0 or (finished and score == 1):
        outcome = "partial"
    else:
        outcome = "fail"
    last = turns[-1].record
    reason = "" if finished else f"turn {len(turns)} ended {last.ended}: {last.detail or ''}"
    return TaskResult(
        id=task.id,
        area=task.area,
        outcome=outcome,
        reason=reason.strip(),
        checks=checks,
        judge_score=score,
        turns=[turn.record for turn in turns],
        conversation_id=conversation_id,
    )


class _Args(argparse.Namespace):
    only: list[str] | None = None
    label: str | None = None
    no_judge: bool = False


async def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="evals.tasks.runner")
    _ = parser.add_argument("--only", nargs="*", help="task ids to run")
    _ = parser.add_argument("--label", help="name for the results file")
    _ = parser.add_argument("--no-judge", action="store_true")
    args = parser.parse_args(argv, namespace=_Args())
    only = args.only

    if not live.configured():
        print(f"Set {', '.join(live.missing())} to the model under test, then `up` again.")
        return 2
    instance = resolve()
    tasks = load_tasks()
    if only:
        unknown = sorted(set(only) - {task.id for task in tasks})
        if unknown:
            print(f"Unknown task ids: {', '.join(unknown)}")
            return 2
        tasks = [task for task in tasks if task.id in only]

    model = os.environ[live.MODEL]
    label = args.label or f"{datetime.now(UTC):%Y-%m-%d-%H%M}-{model.replace('/', '_')}"
    RESULTS_DIR.mkdir(exist_ok=True)
    results_path = RESULTS_DIR / f"{label}.jsonl"
    results: list[TaskResult] = []
    with results_path.open("w") as out:
        for task in tasks:
            print(f"→ {task.id} …", flush=True)
            result = await run_task(
                task,
                base_url=instance.backend_url,
                data_dir=instance.data_dir,
                vm=instance.vm,
                use_judge=not args.no_judge,
            )
            results.append(result)
            _ = out.write(result.model_dump_json() + "\n")
            out.flush()
            print(f"  {result.outcome} {result.reason}", flush=True)
    summary = write_summary(results, model=model, path=results_path.with_suffix(".md"))
    print(f"\nResults: {results_path}\nSummary: {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
