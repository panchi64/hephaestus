"""The meta-loop — what we own *around* the agent's within-turn reasoning.

Two independent mechanisms:

- :class:`LoopBreaker` is **always on**. It watches the tool calls a turn makes
  and aborts when the agent repeats an identical call *and gets an identical
  answer* instead of converging — a no-progress guard the model can't talk its
  way past.
- The **verifier** is opt-in. After a turn produces an answer, a judge (the
  utility model, or an injected stub) decides whether the request was actually
  satisfied; if not, the engine makes a single bounded corrective re-attempt.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import overload

from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_ai.output import OutputSpec
from pydantic_ai.settings import ModelSettings

from prompts.utility import JUDGE_INSTRUCTIONS


@overload
def make_utility_agent(model: Model, *, instructions: str) -> Agent[None, str]: ...
@overload
def make_utility_agent[OutputT](
    model: Model, *, output_type: OutputSpec[OutputT], instructions: str
) -> Agent[None, OutputT]: ...
def make_utility_agent[OutputT](
    model: Model, *, output_type: OutputSpec[OutputT] | None = None, instructions: str
) -> Agent[None, OutputT] | Agent[None, str]:
    """Build a one-shot agent on the cheap utility model for background work — a
    judge, a namer, a summarizer. Centralizes the bare ``Agent`` construction so
    every utility caller picks up the same shape (and any future default:
    instrumentation tag, retries) from one place. Per-call knobs like reasoning-off
    ``model_settings`` are passed to ``agent.run(...)``, not baked in here."""
    if output_type is None:
        return Agent(model, output_type=str, instructions=instructions)
    return Agent(model, output_type=output_type, instructions=instructions)


class LoopDetected(Exception):
    """Raised when a turn repeats an identical tool call without progressing."""

    def __init__(self, tool_name: str) -> None:
        super().__init__(f"repeated the same call to {tool_name!r} without progress")
        self.tool_name: str = tool_name


class LoopBreaker:
    """Counts a turn's identical tool calls **that came back identical**, and trips at a
    repeat threshold.

    **The answer is half the signature, and leaving it out was a real bug rather than a
    simplification.** Identical arguments do not mean identical work: a tool whose result
    depends on state this process does not own returns something new every time it is
    called with the same arguments, and a guard reading arguments alone counts that as
    going nowhere. The browser is the clearest case — `snapshot()` takes no arguments at
    all, so reading the page after the first click, the second and the third is three
    identical calls and the third one killed the turn, in the middle of exactly the
    step-by-step work those tools exist for. It is not only the browser: polling a
    background command with `shell_check_command`, re-reading a file a build is writing,
    and re-listing a directory after creating something in it are the same shape.

    So a call is recorded when it is made and settled when its result arrives, and the
    count advances only while a signature keeps producing the *same* answer. A genuine
    loop still trips on exactly the call it used to: three identical questions with three
    identical answers is a model going in circles, whatever the tool.

    A tool that never returns — a call abandoned by a failed turn — simply leaves its
    pending entry behind; both maps live and die with the turn.
    """

    def __init__(self, *, repeat_threshold: int = 3) -> None:
        #: signature → how many times in a row it has come back with the same answer.
        self._counts: dict[tuple[str, str], int] = {}
        #: signature → the answer it last produced, which the next result is compared to.
        self._answers: dict[tuple[str, str], str] = {}
        #: tool call id → the signature awaiting its result.
        self._pending: dict[str, tuple[str, str]] = {}
        self._threshold: int = repeat_threshold

    def check(self, name: str, args: Mapping[str, object], tool_call_id: str = "") -> None:
        """Record a tool call about to run; raise :class:`LoopDetected` if it is the one
        that would repeat a settled, unchanging answer once too often.

        Raised *before* the call runs, as it always was: by this point the same question
        has already been asked and answered identically ``threshold - 1`` times, so there
        is nothing left to learn from asking it again.
        """
        signature = (name, json.dumps(args, sort_keys=True, default=str))
        if self._counts.get(signature, 0) >= self._threshold - 1:
            raise LoopDetected(name)
        self._pending[tool_call_id] = signature

    def observe(self, tool_call_id: str, answer: object) -> None:
        """Settle the call ``tool_call_id`` with what it returned.

        An answer that differs from the last one this signature produced resets its count:
        the model asked the same question and the world had moved, which is the definition
        of progress this guard is for. A tool result that is not comparable (an image, an
        object with no stable text) hashes by its repr and is treated like any other — at
        worst that reads as progress on something genuinely unchanging, which errs toward
        letting a turn continue rather than killing one that was working.
        """
        signature = self._pending.pop(tool_call_id, None)
        if signature is None:
            return
        fingerprint = _fingerprint(answer)
        if self._answers.get(signature) == fingerprint:
            self._counts[signature] = self._counts.get(signature, 1) + 1
        else:
            self._answers[signature] = fingerprint
            self._counts[signature] = 1


def _fingerprint(answer: object) -> str:
    """A stable, bounded stand-in for one tool result.

    Hashed rather than kept, because these are page snapshots and file contents: holding
    them would make the guard a second copy of the turn's whole tool output, in memory,
    for the length of the turn.
    """
    try:
        rendered = json.dumps(answer, sort_keys=True, default=repr)
    except TypeError, ValueError:  # pragma: no cover — `default=repr` handles the rest
        rendered = repr(answer)
    return hashlib.sha256(rendered.encode("utf-8", "replace")).hexdigest()


class Verdict(BaseModel):
    """A judge's call on whether a response satisfied the request."""

    ok: bool
    reason: str = ""


# A judge inspects (request, answer) and rules on whether the task was done.
Judge = Callable[[str, str], Awaitable[Verdict]]


def make_utility_judge(model: Model, *, model_settings: ModelSettings | None = None) -> Judge:
    """The default judge — asks the given utility model whether the task was
    satisfied. The model is resolved from the registry's ``utility`` role by the
    caller, so the judge itself carries no resolution dependency. ``model_settings``
    carries that model's reasoning-off settings (best-effort, like the namer's): the
    judge is background work that needn't reason, so request it off — a runtime that
    honors the lever answers faster, and the structured ``Verdict`` output is parsed
    from the tool call regardless of any reasoning the model emits anyway."""

    async def judge(request: str, answer: str) -> Verdict:
        agent = make_utility_agent(model, output_type=Verdict, instructions=JUDGE_INSTRUCTIONS)
        result = await agent.run(
            f"Request:\n{request}\n\nResponse:\n{answer}", model_settings=model_settings
        )
        return result.output

    return judge
