"""How a failed tool call is worded for the model: what happened, why, and what to try.

The shape is a compiler diagnostic's, because a model reads it the way a developer reads
rustc: an ``error:`` line that names the failure, then ``cause:`` and ``help:`` lines it
can act on. A failure that is the platform's own fault carries the ``internal`` code,
which is how the live event and the reloaded transcript both tell the operator's card to
mark it.
"""

from __future__ import annotations

import difflib
from collections.abc import Iterable, Sequence

from core.exceptions import FieldValidationError

#: Marks a failure that is a fault in the platform rather than in the call or the
#: environment. ``is_platform_failure`` keys on it.
PLATFORM_CODE = "internal"


def render_tool_error(
    what: str,
    *,
    cause: str | None = None,
    hints: Sequence[str] = (),
    code: str | None = None,
) -> str:
    """One diagnostic block: ``error[code]: what``, then the cause and each hint."""
    lines = [f"error[{code}]: {what}" if code else f"error: {what}"]
    if cause:
        lines.append(f"  cause: {cause}")
    lines.extend(f"  help: {hint}" for hint in hints)
    return "\n".join(lines)


def render_rejection(what: str, exc: FieldValidationError) -> str:
    """A draft the store refused, naming the part of it to fix."""
    return render_tool_error(
        f"{what} was rejected",
        cause=f"`{exc.field}`: {exc}",
        hints=[f"fix `{exc.field}` and call again"],
    )


def is_platform_failure(message: str) -> bool:
    return message.startswith(f"error[{PLATFORM_CODE}]")


def suggest(name: str, candidates: Iterable[str]) -> str | None:
    """A ``did you mean`` hint for the closest candidate, or None when nothing is close."""
    close = difflib.get_close_matches(name, list(candidates), n=1, cutoff=0.6)
    return f"did you mean `{close[0]}`?" if close else None
