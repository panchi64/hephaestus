"""Workspace profiles: the data stays inside what the prompt shows, and a run undoes
exactly what it applied."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from evals.voice.profile import Applied, apply, available, load_profile, remove
from prompts.agent import SKILL_PINNED_LIMIT
from prompts.memory import MEMORY_PINNED_LIMIT


@pytest.mark.parametrize("name", available())
def test_each_profile_fits_what_the_prompt_shows(name: str) -> None:
    profile = load_profile(name)
    assert sum(m.pinned for m in profile.memories) <= MEMORY_PINNED_LIMIT
    assert sum(s.pinned for s in profile.skills) <= SKILL_PINNED_LIMIT


@pytest.mark.parametrize("name", available())
def test_no_profile_sets_an_address_form(name: str) -> None:
    # address-form and address-correction seed their own; a second one would collide.
    for memory in load_profile(name).memories:
        text = f"{memory.content} {memory.description}".lower()
        assert "address" not in text and "call me" not in text, memory.content


def test_an_unknown_profile_names_the_ones_there_are() -> None:
    with pytest.raises(ValueError, match="lived-in"):
        _ = load_profile("nope")


async def test_remove_undoes_apply_in_reverse(tmp_path: Path) -> None:
    calls: list[tuple[str, str, object]] = []
    created = iter(range(1, 1000))

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, body))
        if request.method == "GET":
            return httpx.Response(200, json=[{"name": "mail", "enabled": False}])
        if request.method == "POST":
            return httpx.Response(201, json={"id": f"id{next(created)}"})
        return httpx.Response(204 if request.method == "DELETE" else 200, json={})

    profile = load_profile("lived-in").model_copy(
        update={
            "plugins": ["mail"],
            "memories": load_profile("lived-in").memories[:1],
            "skills": load_profile("lived-in").skills[:1],
            "integrations": load_profile("lived-in").integrations[:1],
            "projects": load_profile("lived-in").projects[:1],
        }
    )
    applied = Applied()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://x"
    ) as http:
        await apply(http, tmp_path, profile, applied)
        assert (
            applied.folder is not None and (applied.folder / "photo-tidy" / "README.md").is_file()
        )
        calls.clear()
        assert await remove(http, applied) == []

    assert [(method, path) for method, path, _ in calls] == [
        ("DELETE", f"/projects/{applied.projects[0]}"),
        ("DELETE", f"/integrations/{applied.integrations[0]}"),
        ("DELETE", f"/skills/{applied.skills[0]}"),
        ("DELETE", f"/memory/{applied.memories[0]}"),
        ("PUT", "/plugins/builtin/mail"),
    ]
    assert calls[-1][2] == {"enabled": False}
    assert not applied.folder.exists()


async def test_remove_reports_what_it_could_not_undo() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    applied = Applied(memories=["m1"])
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://x"
    ) as http:
        assert await remove(http, applied) == ["DELETE /memory/m1: 500"]
