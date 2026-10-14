"""The host file-picker surface — `/host/file-picker`.

Not model-specific: any field that needs an absolute host path goes through here, so the
route is tested on its own terms (availability, an unavailable host, a cancelled dialog)
rather than through whatever feature happens to offer a BROWSE control.
"""

from __future__ import annotations

import pytest

from tests._helpers import client_app
from tests._json import json_body


async def test_file_picker_availability_is_reported_either_way(monkeypatch: pytest.MonkeyPatch):
    # The UI only shows a BROWSE control when this says yes; the path field works
    # regardless, so an unavailable chooser is a clean answer, never an error.
    monkeypatch.setattr("services.host_picker.resolve", lambda: None)
    async with client_app() as (client, _app):
        body = json_body(await client.get("/host/file-picker"))
        assert body["available"] is False
        assert body["reason"]


async def test_opening_a_chooser_on_a_host_without_one_is_a_409(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("services.host_picker.resolve", lambda: None)
    async with client_app() as (client, _app):
        resp = await client.post("/host/file-picker", json={"mode": "directory"})
        assert resp.status_code == 409


async def test_a_cancelled_chooser_returns_no_path(monkeypatch: pytest.MonkeyPatch):
    async def cancelled(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr("services.host_picker.pick", cancelled)
    async with client_app() as (client, _app):
        resp = await client.post("/host/file-picker", json={"mode": "file"})
        assert resp.status_code == 200
        assert json_body(resp)["path"] is None
