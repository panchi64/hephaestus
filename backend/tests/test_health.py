"""Smoke test: the app assembles and serves a liveness response."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app import create_app

from ._json import obj, parse_json


def test_health_ok():
    client = TestClient(create_app())
    resp = client.get("/health")
    assert resp.status_code == 200
    body = obj(parse_json(resp.content))
    assert body["status"] == "ok"
    assert body["version"]
