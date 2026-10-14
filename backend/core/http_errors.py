"""Domain errors → HTTP responses, in one table, installed app-wide.

:mod:`core.exceptions` has always said lower layers raise domain errors and the app maps
them. The mapping half never existed, so every route carried its own: 227 hand-raised
``HTTPException``\\ s, and the same error answered differently depending on which router
caught it. Anything a route *forgot* to catch became a 500 — a ``NotFoundError`` escaping
an unwrapped handler read to the client as "the server is broken" rather than "that isn't
here".

This is the table, and :func:`install_error_handlers` is where it takes effect. A route may
still catch a domain error deliberately — to say something more specific than the domain
message, or because that endpoint genuinely means something different by it. What no route
needs to do any more is restate the answer already written here.

**The statuses, and why.**

- ``NotFoundError`` → **404**. The one every store raises; ownership failures answer the
  same way deliberately (see ``core.db.get_owned``).
- ``PermissionDeniedError`` → **403**.
- ``InvalidInputError`` → **422**. The operator supplied something we can't accept; the
  form that collected it can show the message inline.
- ``DegradedCapabilityError`` → **503**. A capability is unavailable *right now* — no
  embedding endpoint bound, no web browser, no utility model. It is not the caller's
  mistake, so it is never a 4xx, and a retry after the operator wires the capability up is
  the fix. Two routes used to answer 422 here; both turned out to be raising this type for
  what was really invalid input, which is now its own error.
- ``ApprovalRequiredError`` → **409**. The action is real and permitted but needs a
  decision first.
- ``RateLimitedError`` → **429**, with ``Retry-After`` from the error's own field.
- ``SSRFError`` → **422**. The *request* named a target we refuse to reach; nothing
  upstream failed, so this is not a 502.
- ``WebFetchError`` → **502**. A genuine upstream failure fetching one URL.
- ``ModelLoadError`` → **502**. The inference server refused; the hint says what to do.
- ``SpanEditError`` (and its skill subclass) → **409**. The edit's anchor text
  didn't match exactly one span, so the write would have been ambiguous.
- ``SkillValidationError`` → **422**, carrying the offending ``field`` so the operator is
  told which part of the bundle to fix.
- ``HephaestusError`` itself has **no** entry. A base-class catch-all would quietly give a
  tidy status to errors nobody has thought about, which is how a real fault becomes an
  unnoticed 400. Anything unmapped stays a 500 and stays visible.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from core.exceptions import (
    AgentValidationError,
    ApprovalRequiredError,
    DegradedCapabilityError,
    InvalidInputError,
    ModelLoadError,
    NotFoundError,
    PermissionDeniedError,
    PluginValidationError,
    RateLimitedError,
    RoutineValidationError,
    SkillValidationError,
    SpanEditError,
    SSRFError,
    WebFetchError,
)

# Ordered most specific first: the first class an exception is an instance of decides, so
# a subclass that answers differently from its parent must be listed ahead of it.
_STATUSES: tuple[tuple[type[Exception], int], ...] = (
    (NotFoundError, 404),
    (PermissionDeniedError, 403),
    (ApprovalRequiredError, 409),
    (InvalidInputError, 422),
    (SkillValidationError, 422),
    (AgentValidationError, 422),
    (PluginValidationError, 422),
    (RoutineValidationError, 422),
    (SpanEditError, 409),
    (SSRFError, 422),
    (RateLimitedError, 429),
    (DegradedCapabilityError, 503),
    (ModelLoadError, 502),
    (WebFetchError, 502),
)


def status_for(exc: Exception) -> int | None:
    """The status this domain error answers with, or ``None`` if it has no entry — in
    which case it is not ours to translate and should surface as a 500."""
    for error_type, status in _STATUSES:
        if isinstance(exc, error_type):
            return status
    return None


def install_error_handlers(app: FastAPI) -> None:
    """Answer every mapped domain error at the transport boundary.

    Registered per concrete type rather than on ``HephaestusError``, so the set of errors
    that get a considered status is exactly the set written down above — a new domain
    error added without a decision here stays a 500 rather than inheriting one.
    """

    async def handle(_request: Request, exc: Exception) -> JSONResponse:
        status = status_for(exc)
        assert status is not None  # only mapped types are registered
        headers: dict[str, str] = {}
        if isinstance(exc, RateLimitedError):
            # The caller is being asked to wait; tell it how long rather than leaving it
            # to guess or hammer.
            headers["Retry-After"] = str(max(1, round(exc.retry_after_s)))
        return JSONResponse(
            status_code=status, content={"detail": str(exc)}, headers=headers or None
        )

    for error_type, _status in _STATUSES:
        app.add_exception_handler(error_type, handle)
