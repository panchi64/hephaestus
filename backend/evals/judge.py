"""The 0–2 rubric score both live suites grade an answer with."""

from __future__ import annotations

from pydantic import BaseModel
from pydantic_ai.models import Model

from agent.meta import make_utility_agent

_INSTRUCTIONS = (
    "You grade an assistant's answer against a rubric. Score 2 if the answer fully meets "
    "the rubric, 1 if it meets part of it, 0 if it misses it, refuses, gets a fact wrong, "
    "or claims work it does not show. Judge only against the rubric."
)


class _Score(BaseModel):
    score: int
    reason: str = ""


async def judge_answer(model: Model, *, asked: str, rubric: str, answer: str) -> int:
    """How well ``answer`` to ``asked`` meets ``rubric``: 0, 1 or 2."""
    agent = make_utility_agent(model, output_type=_Score, instructions=_INSTRUCTIONS)
    result = await agent.run(f"Asked:\n{asked}\n\nRubric: {rubric}\n\nAnswer:\n{answer}")
    return max(0, min(2, result.output.score))
