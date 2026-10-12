"""Version history: what is recorded, how versions compare, and restoring one."""

from __future__ import annotations

from services.revisions import diff

from ._helpers import client_app


async def _put_file(client, skill_id: str, relpath: str, content: bytes) -> None:
    response = await client.put(
        f"/skills/{skill_id}/files/{relpath}", files={"file": (relpath, content, "text/plain")}
    )
    assert response.status_code == 200, response.text


async def _published_skill(client) -> str:
    created = await client.post(
        "/skills",
        json={
            "name": "release-notes",
            "description": "Draft release notes from a changelog.",
            "body": "Group the merged PRs by area.",
        },
    )
    skill_id = created.json()["id"]
    await _put_file(client, skill_id, "notes.txt", b"first\n")
    assert (await client.post(f"/skills/{skill_id}/publish")).status_code == 200
    return skill_id


async def test_a_draft_has_no_history_until_it_is_published():
    async with client_app() as (client, _app):
        created = await client.post(
            "/skills",
            json={"name": "draft-only", "description": "A draft.", "body": "Steps."},
        )
        skill_id = created.json()["id"]
        await client.patch(f"/skills/{skill_id}", json={"body": "Other steps."})
        assert (await client.get(f"/skills/{skill_id}/revisions")).json() == []

        changes = (await client.get(f"/skills/{skill_id}/revisions/changes")).json()
        assert [change["status"] for change in changes] == ["added"]


async def test_each_change_to_a_published_skill_is_a_version():
    async with client_app() as (client, _app):
        skill_id = await _published_skill(client)
        await client.patch(f"/skills/{skill_id}", json={"body": "Group by area, then by size."})
        await _put_file(client, skill_id, "notes.txt", b"second\n")

        history = (await client.get(f"/skills/{skill_id}/revisions")).json()
        assert [(v["number"], v["note"]) for v in history] == [
            (3, "edited"),
            (2, "edited"),
            (1, "published"),
        ]


async def test_publishing_again_without_changes_adds_no_version():
    async with client_app() as (client, _app):
        skill_id = await _published_skill(client)
        await client.post(f"/skills/{skill_id}/unpublish")
        await client.post(f"/skills/{skill_id}/publish")
        assert len((await client.get(f"/skills/{skill_id}/revisions")).json()) == 1


async def test_restoring_a_skill_brings_back_its_instructions_and_files():
    async with client_app() as (client, _app):
        skill_id = await _published_skill(client)
        await client.patch(f"/skills/{skill_id}", json={"body": "Something else."})
        await _put_file(client, skill_id, "extra.txt", b"new\n")

        since = (await client.get(f"/skills/{skill_id}/revisions/1")).json()
        assert {(c["path"], c["status"]) for c in since} == {
            ("SKILL.md", "changed"),
            ("extra.txt", "added"),
        }

        restored = await client.post(f"/skills/{skill_id}/revisions/1/restore")
        assert restored.status_code == 204
        skill = (await client.get(f"/skills/{skill_id}")).json()
        assert skill["body"].strip() == "Group the merged PRs by area."
        assert [f["relpath"] for f in skill["files"]] == ["notes.txt"]

        history = (await client.get(f"/skills/{skill_id}/revisions")).json()
        assert history[0]["note"] == "restored version 1"
        assert (await client.get(f"/skills/{skill_id}/revisions/1")).json() == []


async def test_deleting_a_skill_drops_its_history():
    async with client_app() as (client, _app):
        skill_id = await _published_skill(client)
        await client.delete(f"/skills/{skill_id}")
        assert (await client.get(f"/skills/{skill_id}/revisions")).status_code == 404


async def test_restoring_an_agent_brings_back_its_instructions_and_model():
    async with client_app() as (client, _app):
        created = await client.post(
            "/agents",
            json={"name": "helper", "description": "Helps.", "instructions": "Be brief."},
        )
        agent_id = created.json()["id"]
        await client.post(f"/agents/{agent_id}/publish")
        await client.patch(
            f"/agents/{agent_id}", json={"instructions": "Be thorough.", "model": "other"}
        )

        await client.post(f"/agents/{agent_id}/revisions/1/restore")
        agent = (await client.get(f"/agents/{agent_id}")).json()
        assert (agent["instructions"], agent["model"]) == ("Be brief.", None)
        notes = [v["note"] for v in (await client.get(f"/agents/{agent_id}/revisions")).json()]
        assert notes == ["restored version 1", "edited", "published"]


async def test_an_unknown_version_is_a_404():
    async with client_app() as (client, _app):
        skill_id = await _published_skill(client)
        response = await client.post(f"/skills/{skill_id}/revisions/9/restore")
        assert response.status_code == 404


def test_a_binary_file_is_compared_without_a_patch():
    changes = diff({"a.bin": b"\xff\x00"}, {"a.bin": b"\xfe\x00", "b.txt": b"hi\n"})
    assert [(c.path, c.status, c.patch is None) for c in changes] == [
        ("a.bin", "changed", True),
        ("b.txt", "added", False),
    ]
