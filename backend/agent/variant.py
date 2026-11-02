"""A prompt variant for a trial: rewrites of the text the model is handed, read from a file.

A trial (``evals/harness/variants/``, ``docs/voice.md`` §16) runs the same scenarios with a
registered prohibition removed, replaced or added. The harness writes each arm's rewrites to
the file ``HEPHAESTUS_PROMPT_VARIANT`` names (empty for the base arm), so arms switch without
a restart. Only a dev instance sets the variable; without it nothing is registered.

Each rewrite is a ``find``/``replace`` pair matched with whitespace collapsed, since a
sentence can be wrapped differently in a docstring than in a register entry. It applies to
the instructions, the system prompt, the tool descriptions and the text a skill load returns
(``skills_open``), on the request only: what is persisted is untouched.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import ClassVar, cast, override

from pydantic import BaseModel, ConfigDict
from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelMessage, ModelRequest, SystemPromptPart, ToolReturnPart
from pydantic_ai.models import ModelRequestContext

from tools import RunDeps

VARIANT_ENV = "HEPHAESTUS_PROMPT_VARIANT"
#: The one tool whose result is prompt text a trial may need to rewrite.
_SKILL_TOOL = "skills_open"


class Rewrite(BaseModel):
    """One edit: ``find`` becomes ``replace`` (empty to remove it)."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    find: str
    replace: str = ""


class _VariantFile(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    rewrite: tuple[Rewrite, ...] = ()


def variant_path() -> Path | None:
    raw = os.environ.get(VARIANT_ENV, "").strip()
    return Path(raw) if raw else None


def _pattern(text: str) -> re.Pattern[str]:
    return re.compile(r"\s+".join(re.escape(word) for word in text.split()))


@dataclass(frozen=True, slots=True)
class _Compiled:
    pattern: re.Pattern[str]
    replace: str


def _literal(replacement: str) -> Callable[[re.Match[str]], str]:
    """A replacement taken as it is, not as a template with backreferences."""
    return lambda _match: replacement


def apply(text: str, rewrites: Sequence[_Compiled]) -> str:
    """``text`` with every rewrite applied; a removal also drops the space it leaves."""
    for rewrite in rewrites:
        if rewrite.replace:
            text = rewrite.pattern.sub(_literal(rewrite.replace), text)
        else:
            text = re.sub(rf"[ \t]?{rewrite.pattern.pattern}", "", text)
    return text


class PromptVariant[DepsT = object](AbstractCapability[DepsT]):
    """Applies the rewrites in ``path`` to every model request; an absent or empty file is
    the base arm. The file is re-read when its mtime changes."""

    def __init__(self, path: Path) -> None:
        self._path: Path = path
        self._stamp: float | None = None
        self._rewrites: tuple[_Compiled, ...] = ()

    def _load(self) -> tuple[_Compiled, ...]:
        try:
            stamp = self._path.stat().st_mtime
        except FileNotFoundError:
            self._stamp, self._rewrites = None, ()
            return ()
        if stamp != self._stamp:
            text = self._path.read_text().strip()
            parsed = _VariantFile.model_validate_json(text) if text else _VariantFile()
            self._rewrites = tuple(
                _Compiled(_pattern(r.find), r.replace) for r in parsed.rewrite
            )
            self._stamp = stamp
        return self._rewrites

    @override
    async def before_model_request(
        self, ctx: RunContext[DepsT], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        del ctx
        rewrites = self._load()
        if not rewrites:
            return request_context
        params = request_context.model_request_parameters
        params = replace(
            params,
            instruction_parts=[
                replace(part, content=apply(part.content, rewrites))
                for part in params.instruction_parts or []
            ]
            or params.instruction_parts,
            function_tools=[
                replace(tool, description=apply(tool.description, rewrites))
                if tool.description
                else tool
                for tool in params.function_tools
            ],
        )
        return replace(
            request_context,
            model_request_parameters=params,
            messages=[_rewrite_message(message, rewrites) for message in request_context.messages],
        )


def _rewrite_message(message: ModelMessage, rewrites: Sequence[_Compiled]) -> ModelMessage:
    if not isinstance(message, ModelRequest):
        return message
    parts = [
        replace(part, content=apply(part.content, rewrites))
        if isinstance(part, SystemPromptPart)
        else replace(part, content=_rewrite_value(part.content, rewrites))
        if isinstance(part, ToolReturnPart) and part.tool_name == _SKILL_TOOL
        else part
        for part in message.parts
    ]
    instructions = apply(message.instructions, rewrites) if message.instructions else None
    return replace(message, parts=parts, instructions=instructions)


def _rewrite_value(value: object, rewrites: Sequence[_Compiled]) -> object:
    if isinstance(value, str):
        return apply(value, rewrites)
    if isinstance(value, dict):
        mapping = cast(dict[object, object], value)
        return {key: _rewrite_value(item, rewrites) for key, item in mapping.items()}
    if isinstance(value, list):
        return [_rewrite_value(item, rewrites) for item in cast(list[object], value)]
    return value


def prompt_variant_capability() -> list[PromptVariant[RunDeps]]:
    """The capability when this process runs trials, else nothing."""
    path = variant_path()
    return [PromptVariant[RunDeps](path)] if path else []
