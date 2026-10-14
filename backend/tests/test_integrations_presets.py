"""Configuring connectors from presets (`INTEG-1`) — the catalog, the row, the sealing."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
from sqlmodel import Session, select

from core.db import init_db, make_engine
from core.exceptions import InvalidInputError, NotFoundError
from core.vault import Vault
from models.external_tool import Integration
from services.external_tools import ExternalPolicyStore
from services.integrations import PRESETS, IntegrationService
from services.integrations.presets import action
from services.integrations.service import STATUS_UNTESTED, _assert_within, _fill_path

from ._helpers import client_app
from ._json import arr, json_body, json_list, obj, s

OWNER = "operator"


async def _service():
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(Path(tempfile.mkdtemp()) / "keyfile.json")
    await vault.setup("pw")
    return engine, vault, IntegrationService(engine, vault, ExternalPolicyStore(engine))


def test_every_preset_is_dialable_and_describes_its_actions():
    """A preset is a promise: an address, an auth shape, a test request, and actions with
    a description the model can choose between."""
    assert PRESETS
    for p in PRESETS:
        assert p.base_url.startswith("https://")
        assert p.auth in ("bearer", "header", "basic", "none")
        assert p.test_path.startswith("/")
        assert p.actions, f"{p.id} exposes nothing to call"
        if p.auth == "header":
            assert p.header_name, f"{p.id} says header auth but names no header"
        for a in p.actions:
            assert a.description.strip()
            assert a.path.startswith("/")
            assert a.method in ("GET", "POST")


async def test_configure_seals_the_credential_and_starts_untested():
    engine, vault, service = await _service()

    view = await service.configure(OWNER, "github", credentials={"token": "ghp_secret"})

    assert view.preset == "github"
    assert view.name == "GitHub"
    assert view.configured is True
    # `INTEG-3` is a separate, deliberate operator action — configuring proves nothing.
    assert view.status == STATUS_UNTESTED
    assert view.last_tested_at is None
    assert {a.name for a in view.actions} == {"get_repo", "list_issues", "create_issue"}
    # Every action starts approval-gated (`AE-3.6`).
    assert all(a.enabled and not a.trusted for a in view.actions)

    with Session(engine) as session:
        row = session.exec(select(Integration).where(Integration.id == view.id)).one()
    assert row.credentials_enc is not None
    assert "ghp_secret" not in row.credentials_enc
    assert json.loads(vault.decrypt_str(row.credentials_enc))["token"] == "ghp_secret"
    assert "ghp_secret" not in json.dumps(view.__dict__, default=str)


async def test_configure_rejects_an_unknown_preset():
    _engine, _vault, service = await _service()

    with pytest.raises(NotFoundError):
        _ = await service.configure(OWNER, "not-a-service")


async def test_an_operator_base_url_overrides_the_preset_and_resets_the_test():
    _engine, _vault, service = await _service()
    view = await service.configure(OWNER, "jira", base_url="https://acme.atlassian.net/")

    assert view.base_url == "https://acme.atlassian.net"

    # Pretend a test had passed, then change the configuration under it.
    await service._record_test(view.id, None)
    assert (await service.get(OWNER, view.id)).status == "ok"

    changed = await service.update(OWNER, view.id, base_url="https://other.atlassian.net")
    # What was proven was proven about the old configuration.
    assert changed.status == STATUS_UNTESTED
    assert changed.last_tested_at is None


async def test_action_policy_is_per_action_and_rejects_an_unknown_one():
    _engine, _vault, service = await _service()
    view = await service.configure(OWNER, "github", credentials={"token": "t"})

    _ = await service.set_action_policy(OWNER, view.id, "get_repo", trusted=True)

    after = {a.name: a for a in (await service.get(OWNER, view.id)).actions}
    assert after["get_repo"].trusted is True
    # Trusting one action left the connector's others exactly as they were.
    assert after["create_issue"].trusted is False
    assert after["list_issues"].trusted is False

    with pytest.raises(NotFoundError):
        _ = await service.set_action_policy(OWNER, view.id, "delete_everything", trusted=True)


async def test_remove_drops_the_connector_and_its_decisions():
    engine, _vault, service = await _service()
    policy = ExternalPolicyStore(engine)
    view = await service.configure(OWNER, "github", credentials={"token": "t"})
    _ = await service.set_action_policy(OWNER, view.id, "get_repo", trusted=True)

    await service.remove(OWNER, view.id)

    assert await service.list(OWNER) == []
    assert await policy.snapshot(OWNER, "integration", view.id) == {}


async def test_only_usable_connectors_are_offered_to_the_agent():
    _engine, _vault, service = await _service()
    unconfigured = await service.configure(OWNER, "github")
    configured = await service.configure(OWNER, "gitlab", credentials={"token": "glpat-x"})
    # A credential-free connector is usable the moment it exists.
    optional = await service.configure(OWNER, "ntfy")

    live = {v.id for v in await service.live_connectors(OWNER)}

    # GitHub requires a credential and hasn't been given one — offering the model a tool
    # that can only 401 wastes a turn.
    assert unconfigured.id not in live
    assert {configured.id, optional.id} <= live

    _ = await service.update(OWNER, configured.id, enabled=False)
    assert configured.id not in {v.id for v in await service.live_connectors(OWNER)}


def test_path_parameters_fill_the_action_and_the_rest_become_query():
    get_repo = action("github", "get_repo")
    assert get_repo is not None
    path, query = _fill_path(get_repo, {"owner": "acme", "repo": "widgets", "state": "open"})

    assert path == "/repos/acme/widgets"
    assert query == {"state": "open"}

    # A parameter can never add a path segment — the action's shape is the preset's.
    escaped, _ = _fill_path(get_repo, {"owner": "a/../b", "repo": "r"})
    assert escaped == "/repos/a%2F..%2Fb/r"

    with pytest.raises(InvalidInputError):
        _ = _fill_path(get_repo, {"owner": "acme"})


def test_a_resolved_url_must_stay_inside_the_configured_connector():
    _assert_within("https://api.github.com", "https://api.github.com/repos/a/b")
    _assert_within("https://gitlab.com/api/v4", "https://gitlab.com/api/v4/projects/1")

    with pytest.raises(InvalidInputError):
        _assert_within("https://api.github.com", "https://evil.example/repos")
    with pytest.raises(InvalidInputError):
        _assert_within("https://gitlab.com/api/v4", "https://gitlab.com/admin")
    with pytest.raises(InvalidInputError):
        _assert_within("https://api.github.com", "https://api.github.com/a/../../b")


async def test_the_rest_surface_lists_presets_configures_and_gates_an_action():
    async with client_app() as (client, _app):
        presets = json_list(await client.get("/integrations/presets"))
        assert {s(obj(p)["id"]) for p in presets} >= {"github", "slack"}

        created = await client.post(
            "/integrations",
            json={"preset": "github", "name": "Work GitHub", "credentials": {"token": "t"}},
        )
        assert created.status_code == 201, created.text
        connector = json_body(created)
        assert connector["name"] == "Work GitHub"
        assert connector["configured"] is True
        assert connector["status"] == "untested"
        # No response shape can hand a credential back out.
        assert "token" not in created.text
        # The surface speaks camelCase, like every other wired seam — pinned here so a
        # field can't quietly revert to snake_case and strand the screen reading it.
        assert connector["credentialRequired"] is True
        assert connector["lastTestedAt"] is None

        gated = await client.patch(
            f"/integrations/{connector['id']}/actions/create_issue", json={"trusted": True}
        )
        assert json_body(gated)["trusted"] is True

        listed = json_list(await client.get("/integrations"))
        by_name = {s(obj(a)["name"]): obj(a) for a in arr(obj(listed[0])["actions"])}
        assert by_name["create_issue"]["trusted"] is True
        assert by_name["get_repo"]["trusted"] is False

        assert (await client.post("/integrations", json={"preset": "nope"})).status_code == 404
        assert (await client.delete(f"/integrations/{connector['id']}")).status_code == 204
        assert (await client.get("/integrations")).json() == []
