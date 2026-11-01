"""Only the operator's own session answers a guarded call; an API token never does."""

from __future__ import annotations

import pytest
from starlette.requests import Request

from core.config import Settings
from core.exceptions import PermissionDeniedError
from routes.deps import app_state, require_operator

from ._helpers import client_app


async def test_the_operator_session_passes():
    async with client_app(auth_enabled=True) as (_, app):
        state = app_state(app)
        state.settings = Settings(auth_enabled=True)
        token = state.auth_manager.issue()
        require_operator(_request(app, f"Bearer {token}"))


async def test_the_session_cookie_passes_too():
    async with client_app(auth_enabled=True) as (_, app):
        state = app_state(app)
        state.settings = Settings(auth_enabled=True)
        token = state.auth_manager.issue()
        require_operator(_request(app, None, cookie=token))


@pytest.mark.parametrize("credential", ["Bearer odyt_abc123", None])
async def test_an_api_token_or_nothing_is_refused(credential: str | None):
    async with client_app(auth_enabled=True) as (_, app):
        app_state(app).settings = Settings(auth_enabled=True)
        with pytest.raises(PermissionDeniedError):
            require_operator(_request(app, credential))


async def test_everyone_is_the_operator_when_auth_is_off():
    async with client_app() as (_, app):
        app_state(app).settings = Settings(auth_enabled=False)
        require_operator(_request(app, "Bearer odyt_abc123"))


def _request(app: object, authorization: str | None, cookie: str | None = None) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if authorization is not None:
        headers.append((b"authorization", authorization.encode()))
    if cookie is not None:
        headers.append((b"cookie", f"hephaestus_session={cookie}".encode()))
    return Request({"type": "http", "headers": headers, "app": app})
