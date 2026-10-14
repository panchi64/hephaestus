"""Taking the model's ``narration`` back off a call before anything tries to validate it.

The other half of the argument ``tools/describe.py`` adds. Every tool is
offered a ``narration`` property — one sentence saying why *this* call, now — so the work
log can show intent beside arguments. No tool function declares it, and nothing below this
file should have to: the sentence is for the operator, not for the capability.

**Why a hook and not a parameter.** Pydantic AI validates a call against the tool
function's own signature with extras forbidden, so an argument no function declares is a
``ValidationError``, which the library turns into a retry prompt — the model is told its
call was malformed for using the property the schema offered it, and does it again. The
obvious alternative fails in the same place: a ``prepared`` stage can put the property on
the wire for free, but the wire is not where the cost lands. Nor is a toolset wrapper
enough — ``WrapperToolset.call_tool`` runs *after* validation, and validation is the thing
that rejects the key. What runs before it is ``before_tool_validate``, which the library
offers every non-output tool call, and that is all this capability implements.

**Idempotence is a requirement, not a nicety.** An approved call is re-validated when the
run resumes, through the same hook, and a hook that assumed it was seeing a fresh call
would be fine here only by luck. Both branches below are written so a second pass over
already-stripped arguments returns them unchanged and untouched.

**Unparseable arguments pass straight through.** A model can emit a JSON fragment this
capability cannot read; the validator has a far better account of what is wrong with it
than a stripper does, and swallowing or rewriting the payload here would replace a precise
error with a confusing one.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import override

from pydantic import JsonValue, TypeAdapter
from pydantic_ai import RunContext, ToolDefinition
from pydantic_ai.capabilities import AbstractCapability, RawToolArgs
from pydantic_ai.messages import ToolCallPart

from .deps import RunDeps
from .describe import NARRATION_ARG, NARRATION_PROPERTY

_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])


def strip_narration(args: Mapping[str, object]) -> dict[str, object]:
    """``args`` without the narration — a plain dict either way.

    Exported because the capability is not the only reader that wants the call's *question*
    rather than its prose. The no-progress guard fingerprints a call by its arguments, and a
    sentence the model rewrites each time would make three identical questions look like
    three different ones (``agent/meta.py``).
    """
    return {name: value for name, value in args.items() if name != NARRATION_ARG}


@dataclass
class NarrationCapability(AbstractCapability[RunDeps]):
    """Removes ``narration`` from a call's raw arguments, and does nothing else.

    No id: it owns no events and no tools, so it never needs to be named. Registered on
    every agent (``agent/factory.py``) rather than per category, because the property is
    added per *tool* by the describing stage and the two have to agree about every tool
    there will ever be — a list here would be the same list twice, rotting separately.
    """

    @override
    async def before_tool_validate(
        self,
        ctx: RunContext[RunDeps],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: RawToolArgs,
    ) -> RawToolArgs:
        if _declares_its_own(tool_def):
            return args
        if isinstance(args, dict):
            return strip_narration(args) if NARRATION_ARG in args else args
        return _strip_from_json(args)


def _declares_its_own(tool_def: ToolDefinition) -> bool:
    """Whether ``narration`` is a real argument of this tool rather than the one we offer.

    The describing stage leaves such a tool's property alone (``toolsets._narrated``), so
    what sits in the schema is either exactly :data:`NARRATION_PROPERTY` or theirs. Keyed on
    the subschema rather than on a name list because the tools that do this are an MCP
    server's or an integration's — discovered per operator, never enumerable here. A key
    the model sent to a tool whose schema has no such property at all is still stripped:
    that is the retry loop this capability exists to prevent.
    """
    schema: dict[str, JsonValue] = tool_def.parameters_json_schema
    properties = schema.get("properties")
    own = properties.get(NARRATION_ARG) if isinstance(properties, dict) else None
    return own is not None and own != NARRATION_PROPERTY


def _strip_from_json(payload: str) -> str:
    """The same removal against a call whose arguments are still a JSON string.

    Providers differ on which of the two shapes they hand over, and the library passes
    through whichever arrived. Re-serialising only when something was actually removed keeps
    a payload nothing needed doing to byte-identical, which is what makes the second pass on
    a resumed call a true no-op rather than a reformat.
    """
    try:
        parsed = _JSON_OBJECT.validate_json(payload)
    except TypeError, ValueError:
        return payload
    if NARRATION_ARG not in parsed:
        return payload
    return json.dumps(strip_narration(parsed))
