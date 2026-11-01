"""Hosted-provider runs: the key's lookup order and the temporary endpoint's lifecycle."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from devkit.instance import DevInstance
from evals.drive.client import DevClient
from evals.drive.keys import provider_key
from evals.voice.collect import HOSTED, Collector


def test_the_environment_beats_the_key_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keys = tmp_path / "keys"
    _ = keys.write_text("# comment\nexport VOICE_TEST_KEY='from-file'\n")
    monkeypatch.delenv("VOICE_TEST_KEY", raising=False)
    assert provider_key("VOICE_TEST_KEY", keys) == "from-file"
    monkeypatch.setenv("VOICE_TEST_KEY", "from-env")
    assert provider_key("VOICE_TEST_KEY", keys) == "from-env"
    monkeypatch.delenv("VOICE_MISSING_KEY", raising=False)
    assert provider_key("VOICE_MISSING_KEY", tmp_path / "absent") is None


async def test_the_temporary_endpoint_is_added_and_removed(tmp_path: Path) -> None:
    calls: list[tuple[str, str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, body))
        if request.method == "POST":
            return httpx.Response(
                201,
                json={"id": "e1", "base_url": "https://api.anthropic.com", "provider": "anthropic"},
            )
        return httpx.Response(204)

    client = DevClient("http://x", transport=httpx.MockTransport(handler))
    collector = Collector(client, DevInstance(name="t", slot=1, root=tmp_path, password="p"))
    try:
        endpoint = await collector.add_endpoint(HOSTED["anthropic"], "sk-test")
        await collector.remove_endpoint(endpoint.id)
    finally:
        await client.aclose()

    assert endpoint.provider == "anthropic"
    assert [(method, path) for method, path, _ in calls] == [
        ("POST", "/models/endpoints"),
        ("DELETE", "/models/endpoints/e1"),
    ]
    assert calls[0][2] == {
        "name": "voice-eval anthropic",
        "provider": "anthropic",
        "base_url": "https://api.anthropic.com",
        "api_key": "sk-test",
        "vision": True,
    }
