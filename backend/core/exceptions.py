"""The application exception hierarchy.

Lower layers raise these domain errors — never transport-layer ``HTTPException``.
:mod:`core.http_errors` maps them to HTTP responses, installed as app-wide handlers, so a
domain error that reaches the transport boundary is answered correctly whether or not the
route that produced it thought to catch it.

A route still catches one of these deliberately — to say something more specific than the
domain message, or to answer with a status this table can't know is right for that
endpoint. What it must not do is restate the *same* answer the table already gives.
"""

from __future__ import annotations


class OdysseusError(Exception):
    """Base for all application errors."""


class NotFoundError(OdysseusError):
    """A requested resource does not exist."""


class InvalidInputError(OdysseusError):
    """The caller supplied something well-formed but unacceptable — a blank name, a path
    that isn't a model of the expected shape, a launch flag the chosen engine can't use.

    Distinct from :class:`DegradedCapabilityError` (a capability is *missing*) and from a
    genuine upstream failure: this is the operator's to correct, and the surface that
    collected it can show the message inline. Its own type because without one, services
    reached for whichever neighbouring error was closest — a blank connector name was
    being reported as a degraded capability, and a mistyped model path as an engine
    fault — and each route then had to translate it back by hand."""


class PermissionDeniedError(OdysseusError):
    """The operator is not permitted to perform this action."""


class ApprovalRequiredError(OdysseusError):
    """A sensitive action needs explicit operator approval before it runs."""


class DegradedCapabilityError(OdysseusError):
    """An optional capability is unavailable; the caller should degrade gracefully."""


class SpanEditError(OdysseusError):
    """A targeted (surgical) edit's ``old_text`` did not match exactly one span — either
    absent (``occurrences == 0``) or ambiguous (``occurrences > 1``). Carries the count so
    the caller can phrase a precise retry for the model. Raised by
    :func:`core.text.replace_unique`, the one implementation behind every surgical edit."""

    def __init__(self, occurrences: int) -> None:
        self.occurrences = occurrences
        super().__init__(
            "old_text was not found" if occurrences == 0
            else f"old_text matched {occurrences} spans"
        )


class SkillSpanError(SpanEditError):
    """A :class:`SpanEditError` on a skill's ``SKILL.md`` body (`SKILL-3`)."""


class FieldValidationError(OdysseusError):
    """Something the operator authored is not well-formed, and this says *which part*.

    The difference from :class:`InvalidInputError` is the audience, not the severity: that
    one is a message a surface shows inline, while this is for a record with several fields
    where "invalid" is useless on its own — the editor has to know which box to mark. The
    routes that raise it turn ``field`` into a 422 body the editor renders verbatim, because
    the backend decides what is valid and the frontend only shows it.
    """

    def __init__(self, field: str, message: str) -> None:
        self.field = field
        super().__init__(message)


class SkillValidationError(FieldValidationError):
    """A skill bundle violates the Agent Skills standard. Carries the offending ``field`` so
    the operator is told *which* part of their bundle to fix, not merely that it failed."""


class AgentValidationError(FieldValidationError):
    """An agent definition the operator or an agent wrote is not usable as one."""


class CommandValidationError(FieldValidationError):
    """A workflow the operator wrote is not usable as a command — a name that could not be
    typed, a missing body, a field over its cap."""


class ModelLoadError(OdysseusError):
    """An inference server refused a request because it couldn't bring the model
    up (e.g. an on-demand cold-load that failed, or a concurrent-load race). The
    message carries an operator-actionable hint — the fix is engine-side (pre-load
    the model, let the server hold more than one), not ours."""


class RateLimitedError(OdysseusError):
    """An action was refused because the caller exceeded its allowed rate. Carries
    the number of seconds to wait before retrying so the route can surface a
    ``Retry-After`` (uploads are rate-limited to protect the service, `UP-4`)."""

    def __init__(self, retry_after_s: float) -> None:
        super().__init__(f"rate limit exceeded; retry in {retry_after_s:.1f}s")
        self.retry_after_s = retry_after_s


class SSRFError(OdysseusError):
    """An outbound request was refused because its target resolves to a
    non-public address (loopback, private, link-local, cloud metadata) or uses a
    disallowed scheme — a server-side request forgery guard."""


class SchemaMigrationError(OdysseusError):
    """Bringing the DB to head failed because its recorded revision and/or its
    physical schema disagree with the migration scripts — typically a dev DB left
    stamped at a deleted or regenerated migration. Carries an operator-actionable
    diagnostic (DB path, stamped vs head revision) in place of the raw Alembic /
    SQLAlchemy traceback; raised at startup, aborting the boot."""


class WebFetchError(OdysseusError):
    """A web fetch failed for a single URL in a way the caller can retry against a
    different source — a network error, a non-OK status, too many redirects, or a
    page with no extractable content. Distinct from a missing capability."""

