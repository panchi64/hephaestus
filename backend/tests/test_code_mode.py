"""Workspace kinds over HTTP: the binding, the branch surface, and the delete gate.

A thread filed under a project starts in that project's folder and moves to a worktree
on its own branch. What these assert is where a new thread lands, which kinds have a
branch to report, and that a delete never silently destroys unmerged work.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from runs import RunRegistry
from runs.run import TERMINAL_STATUSES
from services.conversations import ConversationStore
from services.projects import WorktreeManager
from tests._helpers import app_state, client_app, patch_model_resolution
from tests._json import json_body, s


async def _git(cwd: Path, *args: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    _ = await proc.communicate()


async def _repo(root: Path) -> Path:
    root.mkdir(parents=True)
    _ = (root / "hello.txt").write_text("original\n")
    await _git(root, "git", "init", "-b", "main")
    await _git(root, "git", "add", "-A")
    await _git(root, "git", "-c", "user.name=T", "-c", "user.email=t@e", "commit", "-m", "first")
    return root


async def _settled(app: FastAPI, run_id: str) -> None:
    """Wait for the turn's run to finish. A live run holds the conversation claim, so a
    delete against it answers 409 for a reason that has nothing to do with branches."""
    runs = app_state(app, "runs", RunRegistry)
    for _ in range(400):
        run = runs.get(run_id)
        if run is None or run.status in TERMINAL_STATUSES:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"run {run_id} never settled")


async def create_project(
    client: httpx.AsyncClient, tmp_path: Path, name: str = "work"
) -> dict[str, str]:
    """The created project's text fields (`id`, `rootPath`, `baseRef`, …)."""
    root = await _repo(tmp_path / name)
    resp = await client.post("/projects", json={"name": name, "rootPath": str(root)})
    assert resp.status_code == 201, resp.text
    return {key: value for key, value in json_body(resp).items() if isinstance(value, str)}


def _conversations(app: FastAPI) -> ConversationStore:
    return app_state(app, "conversations", ConversationStore)


class TestTheBinding:
    async def test_an_unknown_project_is_refused_rather_than_filed_nowhere(self):
        async with client_app() as (client, _app):
            resp = await client.post("/chat", json={"prompt": "hi", "project_id": "nope"})
            assert resp.status_code == 404

    async def test_a_project_thread_starts_in_the_projects_own_folder(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        async with client_app() as (client, app):
            project = await create_project(client, tmp_path)
            patch_model_resolution(monkeypatch)
            created = await client.post("/chat", json={"prompt": "hi", "project_id": project["id"]})
            assert created.status_code == 202, created.text
            conversation_id = s(json_body(created)["conversation_id"])

            binding = await _conversations(app).binding(conversation_id)
            assert binding.workspace_kind == "checkout"
            assert binding.project_id == project["id"]

    async def test_an_ephemeral_thread_never_works_in_the_projects_folder(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        async with client_app() as (client, app):
            project = await create_project(client, tmp_path)
            patch_model_resolution(monkeypatch)
            created = await client.post(
                "/chat",
                json={"prompt": "hi", "project_id": project["id"], "ephemeral": True},
            )
            assert created.status_code == 202, created.text
            binding = await _conversations(app).binding(s(json_body(created)["conversation_id"]))
            assert binding.workspace_kind == "sandbox"

    async def test_a_plain_thread_files_itself_under_the_activecreate_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        async with client_app() as (client, app):
            project = await create_project(client, tmp_path)
            _ = await client.post(f"/projects/{project['id']}/activate")
            patch_model_resolution(monkeypatch)
            created = await client.post("/chat", json={"prompt": "hi"})
            conversation_id = s(json_body(created)["conversation_id"])

            binding = await _conversations(app).binding(conversation_id)
            assert binding.project_id == project["id"]
            assert binding.workspace_kind == "checkout"

    async def test_a_thread_with_no_project_works_in_its_sandbox(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        async with client_app() as (client, app):
            patch_model_resolution(monkeypatch)
            created = await client.post("/chat", json={"prompt": "hi"})
            binding = await _conversations(app).binding(s(json_body(created)["conversation_id"]))
            assert binding.workspace_kind == "sandbox"
            assert binding.project_id is None


class TestTheBranchSurface:
    async def test_a_sandbox_thread_has_no_branch(self, monkeypatch: pytest.MonkeyPatch):
        async with client_app() as (client, _app):
            patch_model_resolution(monkeypatch)
            created = await client.post("/chat", json={"prompt": "hi"})
            conversation_id = s(json_body(created)["conversation_id"])
            resp = await client.get(f"/worktrees/{conversation_id}")
            assert resp.status_code == 404

    async def test_a_checkout_thread_has_no_branch_of_its_own(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        async with client_app() as (client, _app):
            project = await create_project(client, tmp_path)
            patch_model_resolution(monkeypatch)
            created = await client.post("/chat", json={"prompt": "hi", "project_id": project["id"]})
            conversation_id = s(json_body(created)["conversation_id"])
            resp = await client.get(f"/worktrees/{conversation_id}")
            assert resp.status_code == 404

    async def test_a_worktree_thread_with_no_work_yet_reports_an_empty_diff(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        async with client_app() as (client, app):
            project = await create_project(client, tmp_path)
            patch_model_resolution(monkeypatch)
            created = await client.post("/chat", json={"prompt": "hi", "project_id": project["id"]})
            conversation_id = s(json_body(created)["conversation_id"])
            assert await _conversations(app).open_worktree(conversation_id)

            body = json_body(await client.get(f"/worktrees/{conversation_id}"))
            # An empty diff, not an error: a thread that hasn't touched a file is a
            # perfectly normal state and the UI should render zeros for it.
            assert body["branch"] == f"heph/{conversation_id}"
            assert body["filesChanged"] == 0
            assert body["patch"] == ""


class TestDeletingAThread:
    async def _with_work(
        self,
        client: httpx.AsyncClient,
        app: FastAPI,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> tuple[dict[str, str], str]:
        project = await create_project(client, tmp_path)
        patch_model_resolution(monkeypatch)
        created = json_body(
            await client.post("/chat", json={"prompt": "hi", "project_id": project["id"]})
        )
        conversation_id = s(created["conversation_id"])
        await _settled(app, s(created["run_id"]))
        assert await _conversations(app).open_worktree(conversation_id)

        # Do what a worktree turn actually does: acquire the worktree and edit files.
        # **No commit** — the agent has no `git commit`, so this is the state every real
        # session is in when the operator opens the diff. Committing here by hand is what
        # made an earlier version of these tests pass against inert code.
        state = await app_state(app, "worktrees", WorktreeManager).acquire(
            project_id=project["id"],
            root=Path(project["rootPath"]),
            base_ref=project["baseRef"],
            conversation_id=conversation_id,
        )
        _ = (state.path / "hello.txt").write_text("changed by the agent\n")
        return project, conversation_id

    async def test_unmerged_work_refuses_the_delete(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        async with client_app() as (client, app):
            _project_view, conversation_id = await self._with_work(
                client, app, tmp_path, monkeypatch
            )
            resp = await client.delete(f"/conversations/{conversation_id}")
            assert resp.status_code == 409
            assert "unmerged work" in resp.text
            # And the thread is still there to merge from.
            assert (await client.get(f"/worktrees/{conversation_id}")).status_code == 200

    async def test_discard_branch_lets_it_through(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        async with client_app() as (client, app):
            _project_view, conversation_id = await self._with_work(
                client, app, tmp_path, monkeypatch
            )
            resp = await client.delete(f"/conversations/{conversation_id}?discardBranch=true")
            assert resp.status_code == 204

    async def test_merge_lands_it_on_the_operators_tree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        async with client_app() as (client, app):
            project, conversation_id = await self._with_work(client, app, tmp_path, monkeypatch)
            root = Path(project["rootPath"])
            # Untouched right up until the operator says so — the point of the design.
            assert (root / "hello.txt").read_text() == "original\n"

            diff = json_body(await client.get(f"/worktrees/{conversation_id}"))
            assert diff["filesChanged"] == 1

            resp = await client.post(f"/worktrees/{conversation_id}/merge")
            assert resp.status_code == 200, resp.text
            assert (root / "hello.txt").read_text() == "changed by the agent\n"

            # ...and now the thread deletes without a fight, because nothing is unmerged.
            assert (await client.delete(f"/conversations/{conversation_id}")).status_code == 204
