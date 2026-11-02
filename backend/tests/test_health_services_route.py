"""The Health page's service rows: shared capabilities plus each enabled endpoint's
last stored connection test."""

from __future__ import annotations

import httpx
import pytest
from pydantic import JsonValue

from routes.deps import app_state
from services import llm

from ._helpers import client_app
from ._json import arr, json_body, obj, parse_json, s


def _rows(resp: httpx.Response) -> dict[str, dict[str, JsonValue]]:
    assert resp.status_code == 200
    return {s(obj(r)["key"]): obj(r) for r in arr(parse_json(resp.content))}


async def test_services_lead_with_the_overview_capabilities():
    async with client_app() as (client, _app):
        overview = json_body(await client.get("/overview"))
        rows = arr(parse_json((await client.get("/health/services")).content))

    capabilities = [obj(r) for r in rows if obj(r)["kind"] == "capability"]
    assert all(r.pop("checked_at") is None and r.pop("kind") for r in capabilities)
    assert capabilities == arr(overview["capabilities"])


async def test_endpoint_rows_follow_the_stored_connection_test(
    monkeypatch: pytest.MonkeyPatch,
):
    async with client_app() as (client, app):
        models = app_state(app).models
        create = models.create_endpoint
        untested = await create("operator", name="Fresh", base_url="http://a/v1")
        ok = await create("operator", name="Good", base_url="http://b/v1", model="m")
        bad = await create("operator", name="Bad", base_url="http://c/v1")
        off = await models.create_endpoint(
            "operator", name="Off", base_url="http://d/v1", enabled=False
        )

        async def _ok(*_args: object, **_kwargs: object) -> None:
            return None

        monkeypatch.setattr(llm, "probe_openai_endpoint", _ok)
        _ = await models.test_endpoint("operator", ok.id)

        async def _refused(*_args: object, **_kwargs: object) -> None:
            raise httpx.ConnectError("refused")

        monkeypatch.setattr(llm, "probe_openai_endpoint", _refused)
        _ = await models.test_endpoint("operator", bad.id)

        rows = _rows(await client.get("/health/services"))
        bad_detail = (await models.get_endpoint("operator", bad.id)).last_error_detail

    assert f"endpoint:{off.id}" not in rows
    fresh = rows[f"endpoint:{untested.id}"]
    assert (fresh["kind"], fresh["label"], fresh["status"]) == ("endpoint", "FRESH", "off")
    assert fresh["checked_at"] is None
    assert fresh["remediation_href"] == "/models"
    good = rows[f"endpoint:{ok.id}"]
    assert (good["status"], good["detail"]) == ("nominal", "m")
    assert good["checked_at"] is not None
    assert good["remediation_href"] is None
    failed = rows[f"endpoint:{bad.id}"]
    assert failed["status"] == "alert"
    assert failed["detail"] == bad_detail
    assert failed["critical"] is False


async def test_services_need_a_session_while_liveness_does_not():
    async with client_app(auth_enabled=True, passphrase=None) as (client, _app):
        assert (await client.get("/health")).status_code == 200
        assert (await client.get("/health/services")).status_code == 401
        assert (await client.get("/runtime")).status_code == 401
