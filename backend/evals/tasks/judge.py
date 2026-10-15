"""The rubric score for a task's final answer, graded by the model under test."""

from __future__ import annotations

from devkit import live
from evals.judge import judge_answer
from services.endpoint_spec import EndpointSpec
from services.llm import build_model

from .spec import Task


async def judge(task: Task, answer: str) -> int:
    endpoint = live.endpoint()
    model = build_model(
        EndpointSpec(
            base_url=endpoint["base_url"] or "",
            model=endpoint["model"] or "",
            api_key=endpoint["api_key"],
        )
    )
    rubric = task.rubric or ""
    return await judge_answer(model, asked="\n\n".join(task.prompts), rubric=rubric, answer=answer)
