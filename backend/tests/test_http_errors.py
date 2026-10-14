"""Domain errors answered at the transport boundary.

``core.exceptions`` has always said lower layers raise domain errors and the app maps
them; until now the mapping half didn't exist, so anything a route forgot to catch became
a 500 and the same error answered differently depending on which router caught it.
"""

from __future__ import annotations

import pytest
from fastapi import APIRouter

from core.exceptions import (
    ApprovalRequiredError,
    DegradedCapabilityError,
    HephaestusError,
    InvalidInputError,
    ModelLoadError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitedError,
    SkillSpanError,
    SkillValidationError,
    SpanEditError,
    SSRFError,
    WebFetchError,
)
from core.http_errors import status_for

from ._helpers import client_app


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (NotFoundError("nope"), 404),
        (PermissionDeniedError("not yours"), 403),
        (ApprovalRequiredError("ask first"), 409),
        (InvalidInputError("a name is required"), 422),
        (SkillValidationError("name", "too long"), 422),
        (SpanEditError(0), 409),
        (SkillSpanError(3), 409),  # a subclass answers with its parent's status
        (SSRFError("refused"), 422),
        (RateLimitedError(4.2), 429),
        (DegradedCapabilityError("no embedding endpoint"), 503),
        (ModelLoadError("pre-load it"), 502),
        (WebFetchError("that page 500ed"), 502),
    ],
)
def test_each_domain_error_has_one_answer(error: Exception, status: int):
    assert status_for(error) == status


def test_the_base_class_is_deliberately_unmapped():
    # A catch-all on HephaestusError would quietly give a tidy status to errors nobody has
    # thought about — which is how a real fault becomes an unnoticed 400. A new domain
    # error stays a 500, and stays visible, until someone decides what it means.
    assert status_for(HephaestusError("something new")) is None
    assert status_for(RuntimeError("not ours at all")) is None


# --- end to end, through a real app ------------------------------------------

_router = APIRouter(prefix="/_errors", tags=["test"])


@_router.get("/not-found")
async def _not_found() -> None:
    raise NotFoundError("conversation 'abc' not found")


@_router.get("/degraded")
async def _degraded() -> None:
    raise DegradedCapabilityError("no utility model is bound")


@_router.get("/rate-limited")
async def _rate_limited() -> None:
    raise RateLimitedError(retry_after_s=4.2)


@_router.get("/unmapped")
async def _unmapped() -> None:
    raise HephaestusError("nobody decided what this means")


async def test_a_route_that_does_not_catch_still_answers_correctly():
    # The whole point of the layer: a store's NotFoundError escaping an unwrapped handler
    # used to read to the client as "the server is broken" rather than "that isn't here".
    async with client_app() as (client, app):
        app.include_router(_router)
        resp = await client.get("/_errors/not-found")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "conversation 'abc' not found"


async def test_a_missing_capability_is_503_not_a_client_error():
    # It is not the caller's mistake and a retry after the operator wires the capability
    # up is the fix. Two routes used to answer 422 here; both turned out to be raising
    # this type for what was really invalid input.
    async with client_app() as (client, app):
        app.include_router(_router)
        resp = await client.get("/_errors/degraded")

    assert resp.status_code == 503
    assert resp.json()["detail"] == "no utility model is bound"


async def test_a_rate_limit_tells_the_caller_how_long_to_wait():
    async with client_app() as (client, app):
        app.include_router(_router)
        resp = await client.get("/_errors/rate-limited")

    assert resp.status_code == 429
    assert resp.headers["retry-after"] == "4"


async def test_an_unmapped_error_is_not_quietly_given_a_status():
    async with client_app() as (client, app):
        app.include_router(_router)
        with pytest.raises(HephaestusError):
            _ = await client.get("/_errors/unmapped")
