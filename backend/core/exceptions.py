"""The application exception hierarchy.

Lower layers raise these domain errors — never transport-layer ``HTTPException``.
:mod:`core.http_errors` maps them to HTTP responses, installed as app-wide handlers, so a
domain error that reaches the transport boundary is answered correctly whether or not the
route that produced it thought to catch it.

A route catches one only to say something more specific than the domain message or to
answer with a status the table can't know suits that endpoint, never to restate the table.
"""

from __future__ import annotations


class HephaestusError(Exception):
    """Base for all application errors.

    ``hint`` is the next step a reader could take, worded for whoever hits the error. A
    tool failure shows it to the model as the diagnostic's ``help:`` line.
    """

    def __init__(self, *args: object, hint: str | None = None) -> None:
        super().__init__(*args)
        self.hint: str | None = hint


class NotFoundError(HephaestusError):
    """A requested resource does not exist."""


class InvalidInputError(HephaestusError):
    """The caller supplied something well-formed but unacceptable: a blank name, a path that
    isn't a model of the expected shape, a launch flag the engine can't use.

    Distinct from :class:`DegradedCapabilityError` (a capability is missing) and from an
    upstream failure: the operator corrects it, and the collecting surface shows the message
    inline."""


class PermissionDeniedError(HephaestusError):
    """The operator is not permitted to perform this action."""


class ApprovalRequiredError(HephaestusError):
    """A sensitive action needs explicit operator approval before it runs."""


class UnmergedWorkError(HephaestusError):
    """Deleting this would throw away commits that were never merged."""


class StaleRewindError(HephaestusError):
    """A file rewind was confirmed against a preview that no longer matches the files."""


class DegradedCapabilityError(HephaestusError):
    """An optional capability is unavailable; the caller should degrade gracefully."""


class SpanEditError(HephaestusError):
    """A surgical edit's ``old_text`` matched not exactly one span: absent (``occurrences ==
    0``) or ambiguous (``> 1``). The count lets the caller phrase a precise retry. Raised by
    :func:`core.text.replace_unique`, behind every surgical edit."""

    def __init__(self, occurrences: int) -> None:
        self.occurrences: int = occurrences
        super().__init__(
            "old_text was not found"
            if occurrences == 0
            else f"old_text matched {occurrences} spans"
        )


class SkillSpanError(SpanEditError):
    """A :class:`SpanEditError` on a skill's ``SKILL.md`` body."""


class FieldValidationError(HephaestusError):
    """Something the operator authored is not well-formed, naming which part.

    Unlike :class:`InvalidInputError` (an inline message), this is for multi-field records
    where the editor must know which box to mark. Routes turn ``field`` into a 422 body the
    editor renders verbatim: the backend decides validity, the frontend shows it.
    """

    def __init__(self, field: str, message: str) -> None:
        self.field: str = field
        super().__init__(message)


class SkillValidationError(FieldValidationError):
    """A skill bundle violates the Agent Skills standard; ``field`` names the part to fix."""


class AgentValidationError(FieldValidationError):
    """An agent definition the operator or an agent wrote is not usable as one."""


class PluginValidationError(FieldValidationError):
    """A plugin's files are not a usable plugin; ``field`` names the part to fix."""


class RoutineValidationError(FieldValidationError):
    """A routine's schedule, agent or prompt is not usable; ``field`` names the part to fix."""


class CommandValidationError(FieldValidationError):
    """A workflow the operator wrote is not usable as a command: an untypeable name, a
    missing body, a field over its cap."""


class ModelLoadError(HephaestusError):
    """An inference server refused a request because it couldn't bring the model up (a failed
    on-demand cold-load, a concurrent-load race). The message carries an operator-actionable
    hint; the fix is engine-side (pre-load the model, let the server hold more than one)."""


class RateLimitedError(HephaestusError):
    """An action was refused for exceeding its rate. Carries the seconds to wait so the route
    can surface a ``Retry-After``."""

    def __init__(self, retry_after_s: float) -> None:
        super().__init__(f"rate limit exceeded; retry in {retry_after_s:.1f}s")
        self.retry_after_s: float = retry_after_s


class SSRFError(HephaestusError):
    """An outbound request was refused because its target resolves to a non-public address
    (loopback, private, link-local, cloud metadata) or uses a disallowed scheme: the SSRF guard."""


class SchemaMigrationError(HephaestusError):
    """Bringing the DB to head failed because its revision or physical schema disagrees with
    the migration scripts (typically a dev DB stamped at a deleted or regenerated
    migration). Carries a diagnostic (DB path, stamped vs head revision) instead of the raw
    traceback; raised at startup, aborting the boot."""


class WebFetchError(HephaestusError):
    """A web fetch failed for one URL in a way the caller can retry against another source
    (network error, non-OK status, too many redirects, no extractable content). Distinct
    from a missing capability."""
