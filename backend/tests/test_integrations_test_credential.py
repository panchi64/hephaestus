"""Proving a connector's credentials (`INTEG-3`) and calling one (`INTEG-2`).

The outbound path is driven through an ``httpx.MockTransport``, so these assert the
things that actually matter about it — where the credential goes, that the resolved URL
is SSRF-guarded and stays inside the operator's connector, that a redirect is refused, and
that a failed test is recorded rather than raised — without touching the network.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from core.db import init_db, make_engine
from core.exceptions import DegradedCapabilityError, InvalidInputError, SSRFError
from core.vault import Vault
from services.external_tools import ExternalPolicyStore
from services.integrations import IntegrationService
from services.integrations.service import MAX_BODY_CHARS, STATUS_ERROR, STATUS_OK

OWNER = "operator"

type _Handler = Callable[[httpx.Request], httpx.Response]


def _recorder(handler: _Handler) -> tuple[httpx.AsyncClient, list[httpx.Request]]:
    """An httpx client over a mock transport, plus the list of requests it saw."""
    seen: list[httpx.Request] = []

    def _handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return httpx.AsyncClient(transport=httpx.MockTransport(_handle)), seen


async def _service(
    handler: _Handler, monkeypatch: pytest.MonkeyPatch
) -> tuple[IntegrationService, list[httpx.Request]]:
    """A service whose outbound calls hit ``handler``. The SSRF guard is neutralized for
    the happy paths (it would resolve DNS for a host these tests never reach); the guard
    itself is exercised by its own case below, against a literal loopback address."""
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(Path(tempfile.mkdtemp()) / "keyfile.json")
    await vault.setup("pw")
    client, seen = _recorder(handler)

    async def _allow(_url: str) -> None:
        return None

    monkeypatch.setattr("services.integrations.service.assert_public_url", _allow)
    service = IntegrationService(engine, vault, ExternalPolicyStore(engine), http_client=client)
    return service, seen


async def test_a_passing_test_records_ok_and_sends_the_credential_as_the_preset_says(
    monkeypatch: pytest.MonkeyPatch,
):
    service, seen = await _service(
        lambda _r: httpx.Response(200, json={"login": "operator"}), monkeypatch
    )
    view = await service.configure(OWNER, "github", credentials={"token": "ghp_x"})

    tested = await service.test(OWNER, view.id)

    assert tested.status == STATUS_OK
    assert tested.last_error is None
    assert tested.last_tested_at is not None
    assert str(seen[0].url) == "https://api.github.com/user"
    assert seen[0].headers["authorization"] == "Bearer ghp_x"
    # Preset-declared headers ride along with every request.
    assert seen[0].headers["accept"] == "application/vnd.github+json"


async def test_a_header_auth_preset_puts_the_token_in_its_own_header(monkeypatch: pytest.MonkeyPatch):
    service, seen = await _service(lambda _r: httpx.Response(200), monkeypatch)
    view = await service.configure(OWNER, "gitlab", credentials={"token": "glpat-x"})

    _ = await service.test(OWNER, view.id)

    assert seen[0].headers["private-token"] == "glpat-x"
    assert "authorization" not in seen[0].headers


async def test_a_basic_auth_preset_encodes_the_pair(monkeypatch: pytest.MonkeyPatch):
    service, seen = await _service(lambda _r: httpx.Response(200), monkeypatch)
    view = await service.configure(
        OWNER, "jira", credentials={"username": "u@example.com", "password": "p"}
    )

    _ = await service.test(OWNER, view.id)

    assert seen[0].headers["authorization"].startswith("Basic ")


async def test_a_rejected_credential_is_recorded_not_raised(monkeypatch: pytest.MonkeyPatch):
    service, _seen = await _service(
        lambda _r: httpx.Response(401, text="Bad credentials"), monkeypatch
    )
    view = await service.configure(OWNER, "github", credentials={"token": "wrong"})

    tested = await service.test(OWNER, view.id)

    # `INTEG-3` exists so the operator learns this *before* relying on the connector —
    # so the answer is a status carrying the service's own words, not an exception.
    assert tested.status == STATUS_ERROR
    assert "401" in (tested.last_error or "")
    assert "Bad credentials" in (tested.last_error or "")


async def test_an_unreachable_service_is_recorded_too(monkeypatch: pytest.MonkeyPatch):
    def _boom(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    service, _seen = await _service(_boom, monkeypatch)
    view = await service.configure(OWNER, "github", credentials={"token": "t"})

    tested = await service.test(OWNER, view.id)

    assert tested.status == STATUS_ERROR
    assert tested.last_error


async def test_calling_an_action_fills_its_path_and_caps_the_response(monkeypatch: pytest.MonkeyPatch):
    body = "x" * (MAX_BODY_CHARS + 500)
    service, seen = await _service(lambda _r: httpx.Response(200, text=body), monkeypatch)
    view = await service.configure(OWNER, "github", credentials={"token": "t"})

    response = await service.call(
        OWNER,
        view.id,
        "list_issues",
        params={"owner": "acme", "repo": "widgets", "state": "open"},
    )

    assert response.ok and response.status == 200
    # A connector is a data source, not a file store — an unbounded body would spend a
    # whole turn's context on one call.
    assert len(response.body) == MAX_BODY_CHARS
    assert str(seen[0].url) == "https://api.github.com/repos/acme/widgets/issues?state=open"


async def test_a_disabled_connector_or_action_refuses_the_call(monkeypatch: pytest.MonkeyPatch):
    service, _seen = await _service(lambda _r: httpx.Response(200), monkeypatch)
    view = await service.configure(OWNER, "github", credentials={"token": "t"})

    _ = await service.set_action_policy(OWNER, view.id, "get_repo", enabled=False)
    with pytest.raises(DegradedCapabilityError):
        _ = await service.call(OWNER, view.id, "get_repo", params={"owner": "a", "repo": "b"})

    _ = await service.update(OWNER, view.id, enabled=False)
    with pytest.raises(DegradedCapabilityError):
        _ = await service.call(OWNER, view.id, "list_issues", params={"owner": "a", "repo": "b"})


async def test_a_body_is_refused_on_an_action_that_does_not_take_one(monkeypatch: pytest.MonkeyPatch):
    service, _seen = await _service(lambda _r: httpx.Response(200), monkeypatch)
    view = await service.configure(OWNER, "github", credentials={"token": "t"})

    with pytest.raises(InvalidInputError):
        _ = await service.call(
            OWNER,
            view.id,
            "get_repo",
            params={"owner": "a", "repo": "b"},
            body={"title": "nope"},
        )


async def test_a_redirect_is_reported_rather_than_followed(monkeypatch: pytest.MonkeyPatch):
    """Following one would carry the operator's credential to wherever it points."""
    service, seen = await _service(
        lambda _r: httpx.Response(302, headers={"Location": "https://evil.example/"}),
        monkeypatch,
    )
    view = await service.configure(OWNER, "github", credentials={"token": "t"})

    response = await service.call(OWNER, view.id, "get_repo", params={"owner": "a", "repo": "b"})

    assert response.status == 302 and not response.ok
    assert len(seen) == 1


async def test_the_ssrf_guard_refuses_a_connector_pointed_at_the_host():
    """The guard runs on the resolved URL, so a base URL walked onto the loopback
    interface never leaves the process."""
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(Path(tempfile.mkdtemp()) / "keyfile.json")
    await vault.setup("pw")
    client, seen = _recorder(lambda _r: httpx.Response(200))
    service = IntegrationService(engine, vault, ExternalPolicyStore(engine), http_client=client)
    view = await service.configure(
        OWNER, "github", base_url="http://127.0.0.1:9", credentials={"token": "t"}
    )

    with pytest.raises(SSRFError):
        _ = await service.call(OWNER, view.id, "get_repo", params={"owner": "a", "repo": "b"})
    assert seen == []
