"""The 0–2 rubric score for a task's final answer, graded by the model under test."""

from __future__ import annotations

from pydantic import BaseModel

from agent.meta import make_utility_agent
from devkit import live
from services.endpoint_spec import EndpointSpec
from services.llm import build_model

from .spec import Task

_INSTRUCTIONS = (
    "You grade an assistant's final answer to a task against a rubric. Score 2 if the "
    "answer fully meets the rubric, 1 if it meets part of it, 0 if it misses it, refuses, "
    "or claims work it does not show. Judge only against the rubric."
)


class _Score(BaseModel):
    score: int
    reason: str = ""


async def judge(task: Task, answer: str) -> int:
    endpoint = live.endpoint()
    model = build_model(
        EndpointSpec(
            base_url=endpoint["base_url"] or "",
            model=endpoint["model"] or "",
            api_key=endpoint["api_key"],
        )
    )
    agent = make_utility_agent(model, output_type=_Score, instructions=_INSTRUCTIONS)
    prompt = "\n\n".join(task.prompts)
    result = await agent.run(f"Task:\n{prompt}\n\nRubric: {task.rubric}\n\nAnswer:\n{answer}")
    return max(0, min(2, result.output.score))
