"""The /skills REST surface: CRUD, publish, surgical edits, and bundle import/export."""

from __future__ import annotations

from typing import Any

import pytest

from ._helpers import client_app
from .test_skills_bundle import SKILL_MD, _zip


def _bundle_zip() -> bytes:
    return _zip(
        {
            "pdf-processing/SKILL.md": SKILL_MD.encode(),
            "pdf-processing/scripts/fill.py": b"print('filling')\n",
        }
    )


async def _create(client, **overrides: str) -> dict[str, Any]:
    payload = {
        "name": "release-notes",
        "description": "Draft release notes from a changelog.",
        "body": "# Release notes\n\nGroup the merged PRs by area.",
    }
    payload.update(overrides)
    response = await client.post("/skills", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


async def test_create_returns_camel_case_and_a_draft():
    async with client_app() as (client, _app):
        created = await _create(client)
        assert created["published"] is False
        assert created["source"] == "authored"
        assert created["fileCount" if "fileCount" in created else "files"] is not None
        assert "createdAt" in created and "updatedAt" in created


async def test_list_returns_camel_case_summaries():
    async with client_app() as (client, _app):
        await _create(client)
        response = await client.get("/skills")
        assert response.status_code == 200
        row = response.json()[0]
        assert row["fileCount"] == 0
        assert row["sizeBytes"] == 0
        assert row["name"] == "release-notes"


async def test_invalid_name_is_a_422_naming_the_field():
    async with client_app() as (client, _app):
        response = await client.post(
            "/skills", json={"name": "Not A Slug", "description": "d", "body": "b"}
        )
        assert response.status_code == 422
        assert response.json()["detail"]["field"] == "name"


async def test_update_and_span_edit():
    async with client_app() as (client, _app):
        created = await _create(client)
        patched = await client.patch(
            f"/skills/{created['id']}", json={"description": "Now different."}
        )
        assert patched.status_code == 200
        assert patched.json()["description"] == "Now different."

        edited = await client.patch(
            f"/skills/{created['id']}/span",
            json={"old_text": "by area", "new_text": "by author"},
        )
        assert edited.status_code == 200
        assert "by author" in edited.json()["body"]


async def test_ambiguous_span_is_a_409():
    async with client_app() as (client, _app):
        created = await _create(client, body="dup dup")
        response = await client.patch(
            f"/skills/{created['id']}/span", json={"old_text": "dup", "new_text": "x"}
        )
        assert response.status_code == 409


async def test_publish_then_unpublish():
    async with client_app() as (client, _app):
        created = await _create(client)
        published = await client.post(f"/skills/{created['id']}/publish")
        assert published.status_code == 200
        assert published.json()["published"] is True

        assert len((await client.get("/skills?published_only=true")).json()) == 1

        await client.post(f"/skills/{created['id']}/unpublish")
        assert (await client.get("/skills?published_only=true")).json() == []


async def test_pin_then_unpin():
    async with client_app() as (client, _app):
        created = await _create(client)
        assert created["pinned"] is False
        pinned = await client.post(f"/skills/{created['id']}/pin")
        assert pinned.status_code == 200
        assert pinned.json()["pinned"] is True
        assert (await client.get("/skills")).json()[0]["pinned"] is True

        await client.post(f"/skills/{created['id']}/unpin")
        assert (await client.get(f"/skills/{created['id']}")).json()["pinned"] is False


async def test_publishing_an_empty_skill_is_a_422():
    async with client_app() as (client, _app):
        created = await _create(client, body="")
        response = await client.post(f"/skills/{created['id']}/publish")
        assert response.status_code == 422
        assert response.json()["detail"]["field"] == "body"


async def test_delete_then_404():
    async with client_app() as (client, _app):
        created = await _create(client)
        assert (await client.delete(f"/skills/{created['id']}")).status_code == 204
        assert (await client.get(f"/skills/{created['id']}")).status_code == 404


@pytest.mark.parametrize("path", ["/skills/nope", "/skills/nope/export"])
async def test_unknown_skill_is_a_404(path):
    async with client_app() as (client, _app):
        assert (await client.get(path)).status_code == 404


async def test_import_a_bundle_lands_a_draft_with_warnings():
    async with client_app() as (client, _app):
        response = await client.post(
            "/skills/import",
            files={"file": ("pdf-processing.zip", _bundle_zip(), "application/zip")},
        )
        assert response.status_code == 201, response.text
        payload = response.json()
        assert payload["skill"]["published"] is False
        assert payload["skill"]["source"] == "imported"
        assert payload["skill"]["allowedTools"] == ["Read", "Bash"]
        assert payload["skill"]["extras"]["when_to_use"].startswith("When the user mentions")
        assert any("allowed-tools" in note for note in payload["warnings"])


async def test_importing_junk_is_a_422_naming_the_field():
    async with client_app() as (client, _app):
        response = await client.post(
            "/skills/import", files={"file": ("x.zip", b"not a zip", "application/zip")}
        )
        assert response.status_code == 422
        assert response.json()["detail"]["field"] == "bundle"


async def test_import_rejects_an_empty_file():
    async with client_app() as (client, _app):
        response = await client.post(
            "/skills/import", files={"file": ("x.zip", b"", "application/zip")}
        )
        assert response.status_code == 422


async def test_export_downloads_a_bundle_that_reimports():
    """The portability contract, end to end over HTTP."""
    async with client_app() as (client, _app):
        imported = (
            await client.post(
                "/skills/import",
                files={"file": ("pdf-processing.zip", _bundle_zip(), "application/zip")},
            )
        ).json()["skill"]

        exported = await client.get(f"/skills/{imported['id']}/export")
        assert exported.status_code == 200
        assert exported.headers["content-type"] == "application/zip"
        assert "pdf-processing.zip" in exported.headers["content-disposition"]

        again = await client.post(
            "/skills/import",
            files={"file": ("pdf-processing.zip", exported.content, "application/zip")},
        )
        assert again.status_code == 201
        reimported = again.json()["skill"]
        assert reimported["name"] == "pdf-processing-2"
        assert reimported["body"] == imported["body"]
        assert reimported["extras"] == imported["extras"]


async def test_bundle_files_can_be_added_read_and_removed():
    async with client_app() as (client, _app):
        created = await _create(client)
        put = await client.put(
            f"/skills/{created['id']}/files/scripts/run.sh",
            files={"file": ("run.sh", b"echo hi", "text/x-shellscript")},
        )
        assert put.status_code == 200
        assert [f["relpath"] for f in put.json()["files"]] == ["scripts/run.sh"]

        got = await client.get(f"/skills/{created['id']}/files/scripts/run.sh")
        assert got.content == b"echo hi"

        removed = await client.delete(f"/skills/{created['id']}/files/scripts/run.sh")
        assert removed.json()["files"] == []


async def test_an_unsafe_bundle_path_is_refused():
    async with client_app() as (client, _app):
        created = await _create(client)
        response = await client.put(
            f"/skills/{created['id']}/files/../escape.sh",
            files={"file": ("escape.sh", b"x", "text/plain")},
        )
        assert response.status_code in {404, 422}


async def test_a_skill_is_edited_as_its_whole_skill_md():
    async with client_app() as (client, _app):
        created = await _create(client)
        await client.patch(f"/skills/{created['id']}", json={"license": "MIT"})
        document = (await client.get(f"/skills/{created['id']}/document")).json()["content"]
        assert document.startswith("---\nname: release-notes\n")
        assert "license: MIT" in document
        edited = document.replace("license: MIT\n", "").replace("by area", "by team")
        saved = await client.put(f"/skills/{created['id']}/document", json={"content": edited})
        assert saved.status_code == 200, saved.text
        body = saved.json()
        assert "by team" in body["body"]
        assert body["license"] is None
        broken = await client.put(
            f"/skills/{created['id']}/document", json={"content": "no frontmatter"}
        )
        assert broken.status_code == 422
        assert broken.json()["detail"]["field"] == "frontmatter"
