"""The /skills REST surface: CRUD, publish, surgical edits, and bundle import/export."""

from __future__ import annotations

import httpx
import pytest
from pydantic import JsonValue

from ._helpers import client_app
from ._json import arr, json_body, json_list, obj, s
from .test_skills_bundle import SKILL_MD, zip_entries


def _bundle_zip() -> bytes:
    return zip_entries(
        {
            "pdf-processing/SKILL.md": SKILL_MD.encode(),
            "pdf-processing/scripts/fill.py": b"print('filling')\n",
        }
    )


async def _create(client: httpx.AsyncClient, **overrides: str) -> dict[str, JsonValue]:
    payload = {
        "name": "release-notes",
        "description": "Draft release notes from a changelog.",
        "body": "# Release notes\n\nGroup the merged PRs by area.",
    }
    payload.update(overrides)
    response = await client.post("/skills", json=payload)
    assert response.status_code == 201, response.text
    return json_body(response)


async def test_create_returns_camel_case_and_a_draft():
    async with client_app() as (client, _app):
        created = await _create(client)
        assert created["published"] is False
        assert created["source"] == "authored"
        assert created["fileCount" if "fileCount" in created else "files"] is not None
        assert "createdAt" in created and "updatedAt" in created


async def test_list_returns_camel_case_summaries():
    async with client_app() as (client, _app):
        _ = await _create(client)
        response = await client.get("/skills")
        assert response.status_code == 200
        row = obj(json_list(response)[0])
        assert row["fileCount"] == 0
        assert row["sizeBytes"] == 0
        assert row["name"] == "release-notes"


async def test_invalid_name_is_a_422_naming_the_field():
    async with client_app() as (client, _app):
        response = await client.post(
            "/skills", json={"name": "Not A Slug", "description": "d", "body": "b"}
        )
        assert response.status_code == 422
        assert obj(json_body(response)["detail"])["field"] == "name"


async def test_update_and_span_edit():
    async with client_app() as (client, _app):
        created = await _create(client)
        patched = await client.patch(
            f"/skills/{created['id']}", json={"description": "Now different."}
        )
        assert patched.status_code == 200
        assert json_body(patched)["description"] == "Now different."

        edited = await client.patch(
            f"/skills/{created['id']}/span",
            json={"old_text": "by area", "new_text": "by author"},
        )
        assert edited.status_code == 200
        assert "by author" in s(json_body(edited)["body"])


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
        assert json_body(published)["published"] is True

        assert len(json_list(await client.get("/skills?published_only=true"))) == 1

        _ = await client.post(f"/skills/{created['id']}/unpublish")
        assert json_list(await client.get("/skills?published_only=true")) == []


async def test_pin_then_unpin():
    async with client_app() as (client, _app):
        created = await _create(client)
        assert created["pinned"] is False
        pinned = await client.post(f"/skills/{created['id']}/pin")
        assert pinned.status_code == 200
        assert json_body(pinned)["pinned"] is True
        assert obj(json_list(await client.get("/skills"))[0])["pinned"] is True

        _ = await client.post(f"/skills/{created['id']}/unpin")
        assert json_body(await client.get(f"/skills/{created['id']}"))["pinned"] is False


async def test_publishing_an_empty_skill_is_a_422():
    async with client_app() as (client, _app):
        created = await _create(client, body="")
        response = await client.post(f"/skills/{created['id']}/publish")
        assert response.status_code == 422
        assert obj(json_body(response)["detail"])["field"] == "body"


async def test_delete_then_404():
    async with client_app() as (client, _app):
        created = await _create(client)
        assert (await client.delete(f"/skills/{created['id']}")).status_code == 204
        assert (await client.get(f"/skills/{created['id']}")).status_code == 404


@pytest.mark.parametrize("path", ["/skills/nope", "/skills/nope/export"])
async def test_unknown_skill_is_a_404(path: str):
    async with client_app() as (client, _app):
        assert (await client.get(path)).status_code == 404


async def test_import_a_bundle_lands_a_draft_with_warnings():
    async with client_app() as (client, _app):
        response = await client.post(
            "/skills/import",
            files={"file": ("pdf-processing.zip", _bundle_zip(), "application/zip")},
        )
        assert response.status_code == 201, response.text
        payload = json_body(response)
        skill = obj(payload["skill"])
        assert skill["published"] is False
        assert skill["source"] == "imported"
        assert skill["allowedTools"] == ["Read", "Bash"]
        assert s(obj(skill["extras"])["when_to_use"]).startswith("When the user mentions")
        assert any("allowed-tools" in s(note) for note in arr(payload["warnings"]))


async def test_importing_junk_is_a_422_naming_the_field():
    async with client_app() as (client, _app):
        response = await client.post(
            "/skills/import", files={"file": ("x.zip", b"not a zip", "application/zip")}
        )
        assert response.status_code == 422
        assert obj(json_body(response)["detail"])["field"] == "bundle"


async def test_import_rejects_an_empty_file():
    async with client_app() as (client, _app):
        response = await client.post(
            "/skills/import", files={"file": ("x.zip", b"", "application/zip")}
        )
        assert response.status_code == 422


async def test_export_downloads_a_bundle_that_reimports():
    """The portability contract, end to end over HTTP."""
    async with client_app() as (client, _app):
        imported = obj(
            json_body(
                await client.post(
                    "/skills/import",
                    files={"file": ("pdf-processing.zip", _bundle_zip(), "application/zip")},
                )
            )["skill"]
        )

        exported = await client.get(f"/skills/{imported['id']}/export")
        assert exported.status_code == 200
        assert exported.headers["content-type"] == "application/zip"
        assert "pdf-processing.zip" in exported.headers["content-disposition"]

        again = await client.post(
            "/skills/import",
            files={"file": ("pdf-processing.zip", exported.content, "application/zip")},
        )
        assert again.status_code == 201
        reimported = obj(json_body(again)["skill"])
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
        assert [obj(f)["relpath"] for f in arr(json_body(put)["files"])] == ["scripts/run.sh"]

        got = await client.get(f"/skills/{created['id']}/files/scripts/run.sh")
        assert got.content == b"echo hi"

        removed = await client.delete(f"/skills/{created['id']}/files/scripts/run.sh")
        assert json_body(removed)["files"] == []


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
        _ = await client.patch(f"/skills/{created['id']}", json={"license": "MIT"})
        document = s(json_body(await client.get(f"/skills/{created['id']}/document"))["content"])
        assert document.startswith("---\nname: release-notes\n")
        assert "license: MIT" in document
        edited = document.replace("license: MIT\n", "").replace("by area", "by team")
        saved = await client.put(f"/skills/{created['id']}/document", json={"content": edited})
        assert saved.status_code == 200, saved.text
        body = json_body(saved)
        assert "by team" in s(body["body"])
        assert body["license"] is None
        broken = await client.put(
            f"/skills/{created['id']}/document", json={"content": "no frontmatter"}
        )
        assert broken.status_code == 422
        assert obj(json_body(broken)["detail"])["field"] == "frontmatter"
