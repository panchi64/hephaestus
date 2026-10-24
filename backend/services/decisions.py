"""The decision capability: typed questions answered with probabilities, not text.

Speaks the TypeSafe wire format (``{state, questions}`` → ``{answers}``) that llama.cpp's
``llama-server`` and LM Studio serve at ``/v1/systemone`` and OpenRouter serves at
``/api/alpha/decisions``. Every failure raises :class:`DecisionUnavailable`; what that
means for the caller is the caller's to say.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated
from urllib.parse import urlsplit

import httpx
from pydantic import (
    BaseModel,
    Field,
    JsonValue,
    TypeAdapter,
    ValidationError,
    ValidationInfo,
    field_validator,
)

from core.exceptions import DegradedCapabilityError
from core.text import clip
from services.endpoint_spec import EndpointSpec

type Probability = Annotated[float, Field(ge=0.0, le=1.0)]

_KEYED: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])
_NAMES: TypeAdapter[list[str]] = TypeAdapter(list[str])


class DecisionUnavailable(DegradedCapabilityError):
    """The decision endpoint did not produce a usable answer to every question."""


@dataclass(frozen=True)
class Noul:
    """A yes/no question; answered with the probability of yes."""

    instructions: str


@dataclass(frozen=True)
class Choice:
    """One of several named options; ``criteria`` maps each option to what it means."""

    instructions: str
    criteria: Mapping[str, str]


@dataclass(frozen=True)
class Score:
    """A position on ordered levels, lowest first."""

    instructions: str
    criteria: Sequence[str]


type Question = Noul | Choice | Score


class NoulAnswer(BaseModel):
    noul: Probability


class ChoiceAnswer(BaseModel):
    choice: str
    probabilities: dict[str, Probability]
    confidence: float | None = None


def _by_position(keyed: dict[str, JsonValue]) -> list[JsonValue]:
    """The values of a map keyed ``"0"`` to ``"n-1"``, in that order."""
    try:
        return [keyed[str(i)] for i in range(len(keyed))]
    except KeyError as exc:
        raise ValueError("score answers must be keyed by level position") from exc


class ScoreAnswer(BaseModel):
    #: The probability-weighted level index, so it can be fractional.
    score: float
    #: The level names the server answered for, lowest first, when it says them.
    legend: list[str] | None = None
    #: Per level, lowest first.
    probabilities: list[Probability]
    confidence: float | None = None

    @field_validator("legend", mode="before")
    @classmethod
    def _legend_in_order(cls, value: object) -> object:
        """The Jev wire keys each level's name by its position (``{"0": name, …}``)."""
        return _by_position(_KEYED.validate_python(value)) if isinstance(value, dict) else value

    @field_validator("probabilities", mode="before")
    @classmethod
    def _levels_in_order(cls, value: object, info: ValidationInfo) -> object:
        """Keyed by level position on the Jev wire, or by level name through ``legend``."""
        if not isinstance(value, dict):
            return value
        keyed = _KEYED.validate_python(value)
        legend = info.data.get("legend")
        if legend is not None and not all(str(i) in keyed for i in range(len(keyed))):
            names = _NAMES.validate_python(legend)
            keyed = {str(i): keyed[name] for i, name in enumerate(names) if name in keyed}
        return _by_position(keyed)


type Answer = NoulAnswer | ChoiceAnswer | ScoreAnswer


class _Response(BaseModel):
    answers: dict[str, Answer]


_RESPONSE = TypeAdapter(_Response)


class _ErrorMessage(BaseModel):
    message: str


class _ErrorDetail(BaseModel):
    msg: str


class _ErrorBody(BaseModel):
    """OpenAI ``{"error": {"message"}}``, bare ``{"error"}``, or FastAPI ``{"detail"}``."""

    error: _ErrorMessage | str | None = None
    detail: str | list[_ErrorDetail] | None = None


_ERROR_BODY = TypeAdapter(_ErrorBody)
_SAID_MAX = 200


def _said(response: httpx.Response) -> str:
    """The server's reason for a failed call, on one line, so the operator can act on it."""
    if response.status_code == 501:
        return " (the model is not a decision model)"
    try:
        body = _ERROR_BODY.validate_json(response.content)
    except ValidationError:
        return ""
    error = body.error.message if isinstance(body.error, _ErrorMessage) else body.error
    detail = body.detail
    if isinstance(detail, list):
        detail = "; ".join(item.msg for item in detail)
    message = " ".join((error or detail or "").split())
    return f": {clip(message, _SAID_MAX)}" if message else ""


def decisions_url(base_url: str) -> str:
    """Where an endpoint serves decisions. A trailing ``/v1`` is dropped, since an endpoint
    configured at ``…/v1`` for chat serves decisions beside it; any prefix before it (a
    reverse proxy's path) is kept."""
    parts = urlsplit(base_url)
    origin = f"{parts.scheme}://{parts.netloc}"
    host = parts.hostname or ""
    if host == "openrouter.ai" or host.endswith(".openrouter.ai"):
        return f"{origin}/api/alpha/decisions"
    prefix = parts.path.rstrip("/").removesuffix("/v1")
    return f"{origin}{prefix}/v1/systemone"


def _wire(question: Question) -> dict[str, JsonValue]:
    match question:
        case Noul(instructions):
            return {"type": "noul", "instructions": instructions}
        case Choice(instructions, criteria):
            return {"type": "choice", "instructions": instructions, "criteria": dict(criteria)}
        case Score(instructions, criteria):
            return {"type": "score", "instructions": instructions, "criteria": list(criteria)}


def _matches(question: Question, answer: Answer) -> bool:
    match question, answer:
        case Noul(), NoulAnswer():
            return True
        case Choice(_, criteria), ChoiceAnswer(choice=choice, probabilities=probabilities):
            return choice in criteria and set(probabilities) <= set(criteria)
        case Score(_, criteria), ScoreAnswer(
            score=score, legend=legend, probabilities=probabilities
        ):
            # Levels a server reordered would be read against the wrong ones.
            return (
                len(probabilities) == len(criteria)
                and 0 <= score <= len(criteria) - 1
                and (legend is None or legend == list(criteria))
            )
        case _:
            return False


async def ask(
    spec: EndpointSpec,
    state: JsonValue,
    questions: Mapping[str, Question],
    *,
    timeout_s: float,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Answer]:
    """Ask every question about ``state`` in one call, and return an answer per question.

    Raises :class:`DecisionUnavailable` unless each question came back with an answer of
    its own type, in range for its criteria.
    """
    body: dict[str, JsonValue] = {
        "model": spec.model,
        "state": state,
        "questions": {name: _wire(question) for name, question in questions.items()},
    }
    headers = {"Authorization": f"Bearer {spec.api_key}"} if spec.api_key else {}
    url = decisions_url(spec.base_url)
    try:
        # A wall-clock bound over the whole call: httpx's own timeouts are per phase.
        async with asyncio.timeout(timeout_s):
            if client is None:
                async with httpx.AsyncClient() as own:
                    response = await own.post(url, json=body, headers=headers, timeout=timeout_s)
            else:
                response = await client.post(url, json=body, headers=headers, timeout=timeout_s)
    except (TimeoutError, httpx.TimeoutException) as exc:
        raise DecisionUnavailable(f"the decision endpoint {url!r} timed out") from exc
    except httpx.HTTPError as exc:
        raise DecisionUnavailable(f"couldn't reach the decision endpoint {url!r}") from exc
    if response.status_code != 200:
        raise DecisionUnavailable(
            f"the decision endpoint {url!r} answered {response.status_code}{_said(response)}"
        )
    try:
        parsed = _RESPONSE.validate_json(response.content)
    except ValidationError as exc:
        raise DecisionUnavailable(f"the decision endpoint {url!r} sent no usable answers") from exc
    answers = parsed.answers
    for name, question in questions.items():
        answer = answers.get(name)
        if answer is None or not _matches(question, answer):
            raise DecisionUnavailable(f"the decision endpoint {url!r} did not answer {name!r}")
    return {name: answers[name] for name in questions}
