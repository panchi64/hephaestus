"""Forking a thread from one of its messages.

The thing that separates a fork from the branch operations already in the store
(regenerate/edit/rewind) is that it produces a **second conversation**. So what these
assert, mostly, is that the source is left alone: same leaf, same message ids, same
history. A fork that quietly repositioned the original would look identical in the new
thread and be a data-loss bug in the old one.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import FastAPI
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

from core.db import init_db, make_engine
from core.vault import Vault
from runs import RunRegistry
from runs.run import TERMINAL_STATUSES
from services.conversations import ConversationStore
from services.projects.worktree import WorktreeManager
from tests._helpers import app_state, client_app, patch_model_resolution
from tests._json import arr, json_body, obj, s

OWNER = "operator"


async def _store(tmp_path: Path) -> ConversationStore:
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(tmp_path / "keyfile.json")
    await vault.setup("pw")
    return ConversationStore(engine, vault)


def _user(text: str) -> ModelRequest:
    return ModelRequest(parts=[UserPromptPart(content=text)])


def _assistant(text: str) -> ModelResponse:
    return ModelResponse(parts=[TextPart(content=text)])


async def _thread(store: ConversationStore) -> tuple[str, list[str]]:
    """Three turns, and the branch-node ids of each."""
    conversation_id = await store.create_conversation(OWNER, title="Original")
    for n in range(3):
        _ = store.record(conversation_id, [_user(f"q{n}"), _assistant(f"a{n}")])
    tree = await store._tree(conversation_id)  # noqa: SLF001 — the structure is the subject
    return conversation_id, [node.id for node in tree.active_path()]


class TestTheCopy:
    async def test_carries_history_through_the_chosen_turn(self, tmp_path: Path):
        store = await _store(tmp_path)
        source, ids = await _thread(store)
        # Fork at the second user turn (index 2 on the path: q0 a0 q1 …).
        forked = await store.fork(source, ids[2], OWNER)
        assert forked is not None

        texts = [m.content for m in await store.messages_view(forked)]
        assert texts == ["q0", "a0", "q1"]

    async def test_the_source_is_untouched(self, tmp_path: Path):
        store = await _store(tmp_path)
        source, ids = await _thread(store)
        before = await store._tree(source)  # noqa: SLF001
        leaf_before = before.active_leaf_id

        _ = await store.fork(source, ids[2], OWNER)

        after = await store._tree(source)  # noqa: SLF001
        assert after.active_leaf_id == leaf_before
        assert [n.id for n in after.active_path()] == ids

    async def test_the_copies_carry_new_ids(self, tmp_path: Path):
        store = await _store(tmp_path)
        source, ids = await _thread(store)
        forked = await store.fork(source, ids[2], OWNER)
        assert forked is not None

        tree = await store._tree(forked)  # noqa: SLF001
        copied = [n.id for n in tree.active_path()]
        # Shared ids would make the two threads alias each other's rows — the message
        # table is keyed on id, not on (conversation, id).
        assert not set(copied) & set(ids)
        # ...and they are re-parented into a chain of their own, root-first.
        assert tree.nodes[copied[0]].parent_id is None
        assert tree.nodes[copied[1]].parent_id == copied[0]

    async def test_forking_an_assistant_turn_carries_the_whole_turn(self, tmp_path: Path):
        store = await _store(tmp_path)
        source, ids = await _thread(store)
        # `a0` is at index 1; the fork should include it, not stop before it.
        forked = await store.fork(source, ids[1], OWNER)
        assert forked is not None
        assert [m.content for m in await store.messages_view(forked)] == ["q0", "a0"]

    async def test_an_off_path_message_is_refused(self, tmp_path: Path):
        store = await _store(tmp_path)
        source, _ids = await _thread(store)
        assert await store.fork(source, "not-a-message", OWNER) is None

    async def test_the_fork_names_its_source(self, tmp_path: Path):
        store = await _store(tmp_path)
        source, ids = await _thread(store)
        forked = await store.fork(source, ids[2], OWNER)
        assert forked is not None
        summary = await store.get_summary(forked, OWNER)
        assert summary is not None
        # An auto-title would spend a model request restating what the source already
        # says, and the operator forked a *specific* thread.
        assert summary.title == "Fork of Original"

    async def test_a_forked_untitled_thread_stays_untitled(self, tmp_path: Path):
        store = await _store(tmp_path)
        source = await store.create_conversation(OWNER)
        _ = store.record(source, [_user("q"), _assistant("a")])
        tree = await store._tree(source)  # noqa: SLF001
        forked = await store.fork(source, tree.active_path()[0].id, OWNER)
        assert forked is not None
        summary = await store.get_summary(forked, OWNER)
        assert summary is not None and summary.title is None


class TestTheBinding:
    async def test_the_fork_inherits_project_and_workspace_kind(self, tmp_path: Path):
        store = await _store(tmp_path)
        source = await store.create_conversation(
            OWNER, title="T", project_id="proj-1", workspace_kind="worktree"
        )
        _ = store.record(source, [_user("q"), _assistant("a")])
        tree = await store._tree(source)  # noqa: SLF001

        forked = await store.fork(source, tree.active_path()[0].id, OWNER)
        assert forked is not None
        binding = await store.binding(forked)
        # A worktree fork that came back as a sandbox thread would silently drop the operator
        # into a different workspace with the same transcript.
        assert (binding.workspace_kind, binding.project_id) == ("worktree", "proj-1")


# --- over HTTP, and the code fork's branch --------------------------------------------


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
    runs = app_state(app, "runs", RunRegistry)
    for _ in range(400):
        run = runs.get(run_id)
        if run is None or run.status in TERMINAL_STATUSES:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"run {run_id} never settled")


class TestTheRoute:
    async def test_it_returns_the_new_thread_not_the_old_one(self, monkeypatch: pytest.MonkeyPatch):
        async with client_app() as (client, app):
            patch_model_resolution(monkeypatch)
            created = json_body(await client.post("/chat", json={"prompt": "hello"}))
            await _settled(app, s(created["run_id"]))
            source = s(created["conversation_id"])

            detail = json_body(await client.get(f"/conversations/{source}"))
            first = s(obj(arr(detail["messages"])[0])["id"])

            resp = await client.post(f"/conversations/{source}/messages/{first}/fork")
            assert resp.status_code == 200, resp.text
            body = json_body(resp)
            # The client navigates straight to it, so the *fork* has to come back.
            assert body["id"] != source
            assert (await client.get(f"/conversations/{source}")).status_code == 200

    async def test_an_unknown_message_is_a_404(self, monkeypatch: pytest.MonkeyPatch):
        async with client_app() as (client, app):
            patch_model_resolution(monkeypatch)
            created = json_body(await client.post("/chat", json={"prompt": "hello"}))
            await _settled(app, s(created["run_id"]))
            resp = await client.post(
                f"/conversations/{created['conversation_id']}/messages/nope/fork"
            )
            assert resp.status_code == 404

    async def test_a_worktree_fork_branches_from_the_sources_branch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        async with client_app() as (client, app):
            root = await _repo(tmp_path / "work")
            project = json_body(
                await client.post("/projects", json={"name": "work", "rootPath": str(root)})
            )
            patch_model_resolution(monkeypatch)
            created = json_body(
                await client.post(
                    "/chat",
                    json={"prompt": "hello", "project_id": project["id"]},
                )
            )
            await _settled(app, s(created["run_id"]))
            source = s(created["conversation_id"])
            assert await app_state(app, "conversations", ConversationStore).open_worktree(source)

            # The source does some work. It does **not** commit — the agent has no
            # `git commit`; reading the branch is what snapshots it.
            state = await app_state(app, "worktrees", WorktreeManager).acquire(
                project_id=s(project["id"]),
                root=root,
                base_ref="main",
                conversation_id=source,
            )
            _ = (state.path / "new.txt").write_text("from the source thread\n")
            assert json_body(await client.get(f"/worktrees/{source}"))["filesChanged"] == 1

            detail = json_body(await client.get(f"/conversations/{source}"))
            first = s(obj(arr(detail["messages"])[0])["id"])
            forked = json_body(await client.post(f"/conversations/{source}/messages/{first}/fork"))

            # The fork's transcript describes `new.txt`, so its branch has to contain it —
            # branching from `main` would hand it a tree its own history contradicts.
            diff = json_body(await client.get(f"/worktrees/{forked['id']}"))
            assert diff["branch"] == f"heph/{forked['id']}"
            assert "from the source thread" in s(diff["patch"])
