"""Putting a project's files back to before a turn: the per-turn ledger, the dry run and its
token, and the edit / regenerate routes that rewind first."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from pydantic_ai import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from sqlalchemy.exc import OperationalError
from sqlmodel import Session

from core.container import ServiceContainer
from core.db import init_db, make_engine
from core.exceptions import StaleRewindError
from core.vault import Vault
from routes.deps import app_state
from runs import Run
from runs.stream import RunStream
from services.home.backend import Mount
from services.home.changes import ChangeLog, RewindPlan, ThreadChanges
from services.home.paths import Place
from tools.deps import RunDeps
from tools.home import change_log

from ._helpers import client_app, patch_model_resolution
from ._json import arr, json_body, obj, s
from .test_branching import fresh_store

OWNER = "operator"


async def _store(tmp_path: Path) -> ThreadChanges:
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(tmp_path / "keyfile.json")
    await vault.setup("pw")
    return ThreadChanges(engine, vault, max_kept_bytes=1024)


async def _turn(
    store: ThreadChanges,
    root: Path,
    turn: str | None,
    edits: dict[str, str | None],
    conv: str = "c1",
) -> None:
    for rel, content in edits.items():
        await store.before(OWNER, conv, "p1", root, [rel], prompt_message_id=turn)
        target = root / rel
        if content is None:
            target.unlink()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            _ = target.write_text(content)
        await store.after(OWNER, conv, "p1", root, [rel], prompt_message_id=turn)


async def _rewind(
    store: ThreadChanges, root: Path, turns: list[str], conv: str = "c1"
) -> tuple[RewindPlan, RewindPlan]:
    roots = {"p1": root}
    plan = await store.rewind_plan(OWNER, conv, turns, roots)
    done = await store.rewind_files(OWNER, conv, turns, roots, plan.preview_token)
    return plan, done


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    _ = (root / "app.py").write_text("zero\n")
    return root


async def test_a_rewind_to_the_first_turn_restores_the_file_from_before_it(tmp_path: Path):
    root = _project(tmp_path)
    store = await _store(tmp_path)
    await _turn(store, root, "t1", {"app.py": "one\n", "new.py": "made\n"})
    await _turn(store, root, "t2", {"app.py": "two\n"})

    plan, _ = await _rewind(store, root, ["t1", "t2"])

    assert [f.path for f in plan.restore] == ["app.py"]
    assert [f.path for f in plan.delete] == ["new.py"]
    assert (root / "app.py").read_text() == "zero\n"
    assert not (root / "new.py").exists()


async def test_a_rewind_to_the_second_turn_restores_only_its_change(tmp_path: Path):
    root = _project(tmp_path)
    store = await _store(tmp_path)
    await _turn(store, root, "t1", {"app.py": "one\n", "new.py": "made\n"})
    await _turn(store, root, "t2", {"app.py": "two\n"})

    _ = await _rewind(store, root, ["t2"])

    assert (root / "app.py").read_text() == "one\n"
    assert (root / "new.py").read_text() == "made\n"


async def test_a_hand_edited_file_is_refused_and_the_others_restored(tmp_path: Path):
    root = _project(tmp_path)
    _ = (root / "other.py").write_text("other\n")
    store = await _store(tmp_path)
    await _turn(store, root, "t1", {"app.py": "one\n", "other.py": "changed\n"})
    _ = (root / "app.py").write_text("by hand\n")

    plan, _ = await _rewind(store, root, ["t1"])

    assert [(c.path, c.reason.startswith("it has changed")) for c in plan.conflicts] == [
        ("app.py", True)
    ]
    assert [f.path for f in plan.restore] == ["other.py"]
    assert (root / "app.py").read_text() == "by hand\n"
    assert (root / "other.py").read_text() == "other\n"


async def test_a_file_too_large_to_keep_is_reported_unrestorable(tmp_path: Path):
    root = _project(tmp_path)
    _ = (root / "big.bin").write_text("x" * 2048)
    store = await _store(tmp_path)
    await _turn(store, root, "t1", {"big.bin": "small\n"})

    plan, _ = await _rewind(store, root, ["t1"])

    assert [f.path for f in plan.unrestorable] == ["big.bin"]
    assert (root / "big.bin").read_text() == "small\n"


async def test_the_ledger_rewinds_a_tagged_childs_edit_with_its_parents_turn(tmp_path: Path):
    root = _project(tmp_path)
    store = await _store(tmp_path)
    parent = ChangeLog(store, owner_id=OWNER, conversation_id="c1", prompt_message_id="t1")
    # The sub-agent files under the parent thread with the launching turn's prompt.
    child = ChangeLog(store, owner_id=OWNER, conversation_id="c1", prompt_message_id="t1")
    mount = Mount(Place("projects", "api"), root, writable=True, project_id="p1")
    await parent.before(mount, ["app.py"])
    _ = (root / "app.py").write_text("parent\n")
    await parent.after(mount, ["app.py"])
    await child.before(mount, ["child.py"])
    _ = (root / "child.py").write_text("child\n")
    await child.after(mount, ["child.py"])

    _ = await _rewind(store, root, ["t1"])

    assert (root / "app.py").read_text() == "zero\n"
    assert not (root / "child.py").exists()


async def test_a_spawned_agents_deps_file_changes_under_the_launching_turn(tmp_path: Path):
    store = await _store(tmp_path)
    caps = ServiceContainer()
    caps.add(store)
    run = Run(id="r", kind="subagent", owner_id=OWNER, stream=RunStream())
    run.prompt_message_id = "child-prompt"
    child = RunDeps(
        run=run,
        owner_id=OWNER,
        caps=caps,
        conversation_id="child",
        workspace_key="parent",
        ledger_prompt_message_id="parent-prompt",
    )
    log = change_log(child)
    assert log is not None
    mount = Mount(Place("projects", "api"), tmp_path, writable=True, project_id="p1")
    await log.before(mount, ["a.txt"])
    _ = (tmp_path / "a.txt").write_text("a")
    await log.after(mount, ["a.txt"])

    plan = await store.rewind_plan(OWNER, "parent", ["parent-prompt"], {"p1": tmp_path})
    assert [f.path for f in plan.delete] == ["a.txt"]
    unfiled = await store.rewind_plan(OWNER, "parent", ["child-prompt"], {"p1": tmp_path})
    assert unfiled.delete == []


async def test_a_regenerates_two_runs_rewind_together(tmp_path: Path):
    root = _project(tmp_path)
    store = await _store(tmp_path)
    await _turn(store, root, "t1", {"app.py": "first run\n"})
    await _turn(store, root, "t1", {"app.py": "second run\n"})

    _ = await _rewind(store, root, ["t1"])

    assert (root / "app.py").read_text() == "zero\n"


async def test_the_dry_run_writes_nothing(tmp_path: Path):
    root = _project(tmp_path)
    store = await _store(tmp_path)
    await _turn(store, root, "t1", {"app.py": "one\n"})

    plan = await store.rewind_plan(OWNER, "c1", ["t1"], {"p1": root})

    assert [f.path for f in plan.restore] == ["app.py"]
    assert (root / "app.py").read_text() == "one\n"
    assert [v.reverted for v in await store.list(OWNER, "c1")] == [False]


async def test_a_stale_token_is_refused(tmp_path: Path):
    root = _project(tmp_path)
    store = await _store(tmp_path)
    await _turn(store, root, "t1", {"app.py": "one\n"})
    plan = await store.rewind_plan(OWNER, "c1", ["t1"], {"p1": root})
    _ = (root / "app.py").write_text("by hand\n")

    with pytest.raises(StaleRewindError):
        _ = await store.rewind_files(OWNER, "c1", ["t1"], {"p1": root}, plan.preview_token)
    assert (root / "app.py").read_text() == "by hand\n"


async def test_thread_revert_still_goes_back_to_before_the_first_turn(tmp_path: Path):
    root = _project(tmp_path)
    store = await _store(tmp_path)
    await _turn(store, root, "t1", {"app.py": "one\n"})
    await _turn(store, root, "t2", {"app.py": "two\n"})

    [view] = await store.list(OWNER, "c1")
    assert view.status == "modified"
    report = await store.revert(OWNER, "c1", {"p1": root})

    assert report.reverted == ["app.py"]
    assert (root / "app.py").read_text() == "zero\n"


async def _conversation_with_a_turn(
    client: httpx.AsyncClient, app: FastAPI, root: Path
) -> tuple[str, str, str]:
    project = json_body(
        await client.post("/projects", json={"name": "work", "rootPath": str(root)})
    )
    project_id = s(project["id"])
    conv = await app_state(app).conversations.create_conversation(OWNER, project_id=project_id)
    created = json_body(await client.post("/chat", json={"prompt": "go", "conversation_id": conv}))
    async with client.stream("GET", f"/runs/{s(created['run_id'])}/events") as resp:
        async for _ in resp.aiter_lines():
            pass
    detail = json_body(await client.get(f"/conversations/{conv}"))
    messages = [obj(m) for m in arr(detail["messages"])]
    user = next(m for m in messages if m["role"] == "user")
    assistant = next(m for m in messages if m["role"] == "assistant")
    store = app_state(app).capabilities.get(ThreadChanges)
    await store.before(OWNER, conv, project_id, root, ["app.py"], prompt_message_id=s(user["id"]))
    _ = (root / "app.py").write_text("changed\n")
    await store.after(OWNER, conv, project_id, root, ["app.py"], prompt_message_id=s(user["id"]))
    return conv, s(user["id"]), s(assistant["id"])


async def test_the_rewind_routes_preview_then_confirm(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    patch_model_resolution(monkeypatch)
    root = _project(tmp_path)
    async with client_app() as (client, app):
        conv, _user, assistant = await _conversation_with_a_turn(client, app, root)

        preview = json_body(
            await client.get(f"/conversations/{conv}/changes/rewind?messageId={assistant}")
        )
        assert [obj(f)["path"] for f in arr(preview["restore"])] == ["app.py"]
        assert preview["delete"] == [] and preview["conflicts"] == []
        assert (root / "app.py").read_text() == "changed\n"

        _ = (root / "app.py").write_text("by hand\n")
        stale = await client.post(
            f"/conversations/{conv}/changes/rewind",
            json={"messageId": assistant, "previewToken": preview["previewToken"]},
        )
        assert stale.status_code == 409
        assert (root / "app.py").read_text() == "by hand\n"

        _ = (root / "app.py").write_text("changed\n")
        done = await client.post(
            f"/conversations/{conv}/changes/rewind",
            json={"messageId": assistant, "previewToken": preview["previewToken"]},
        )
        assert done.status_code == 200
        assert (root / "app.py").read_text() == "zero\n"


async def test_regenerate_with_a_token_rewinds_before_the_leaf_moves(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    patch_model_resolution(monkeypatch)
    root = _project(tmp_path)
    async with client_app() as (client, app):
        conv, _user, assistant = await _conversation_with_a_turn(client, app, root)
        preview = json_body(
            await client.get(f"/conversations/{conv}/changes/rewind?messageId={assistant}")
        )

        bad = await client.post(
            "/chat/regenerate",
            json={
                "conversation_id": conv,
                "message_id": assistant,
                "restore_files": {"preview_token": "nope"},
            },
        )
        assert bad.status_code == 409
        assert (root / "app.py").read_text() == "changed\n"

        ok = await client.post(
            "/chat/regenerate",
            json={
                "conversation_id": conv,
                "message_id": assistant,
                "restore_files": {"preview_token": preview["previewToken"]},
            },
        )
        assert ok.status_code == 202
        assert (root / "app.py").read_text() == "zero\n"


async def test_edit_without_a_token_leaves_the_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    patch_model_resolution(monkeypatch)
    root = _project(tmp_path)
    async with client_app() as (client, app):
        conv, user, _assistant = await _conversation_with_a_turn(client, app, root)
        edited = await client.post(
            "/chat/edit", json={"conversation_id": conv, "message_id": user, "prompt": "again"}
        )
        assert edited.status_code == 202
        assert (root / "app.py").read_text() == "changed\n"


async def test_the_turn_prompts_run_from_the_named_turn_to_the_leaf(tmp_path: Path):
    store, _ = await fresh_store(tmp_path)
    conv = await store.create_conversation(OWNER)
    ids: list[str] = []
    for text in ("one", "two", "three"):
        request = ModelRequest(parts=[UserPromptPart(text)])
        response = ModelResponse(parts=[TextPart("ok")])
        ids.extend(store.record(conv, [request, response]))
    prompts = ids[0::2]

    assert await store.turn_prompt_ids(conv, prompts[1]) == prompts[1:]
    assert await store.turn_prompt_ids(conv, ids[3]) == prompts[1:]
    assert await store.turn_prompt_ids(conv, "missing") is None


async def test_a_mid_run_aside_belongs_to_the_turn_it_interrupted(tmp_path: Path):
    store, _ = await fresh_store(tmp_path)
    conv = await store.create_conversation(OWNER)
    ids = store.record(
        conv,
        [
            ModelRequest(parts=[UserPromptPart("go")]),
            ModelResponse(parts=[ToolCallPart("t", {}, tool_call_id="c1")]),
            ModelRequest(parts=[ToolReturnPart("t", "r", tool_call_id="c1")]),
            ModelRequest(parts=[UserPromptPart("aside")]),
            ModelResponse(parts=[TextPart("done")]),
        ],
    )
    request, aside = ids[0], ids[3]

    assert await store.turn_prompt_ids(conv, ids[4]) == [request, aside]
    assert await store.turn_prompt_ids(conv, aside) == [request, aside]


@pytest.mark.parametrize("order", [(None, "t1"), ("t1", None)])
async def test_a_none_row_and_a_tagged_row_for_one_file_revert_together(
    tmp_path: Path, order: tuple[str | None, str | None]
):
    root = _project(tmp_path)
    store = await _store(tmp_path)
    await _turn(store, root, order[0], {"app.py": "one\n"})
    await _turn(store, root, order[1], {"app.py": "two\n"})

    [view] = await store.list(OWNER, "c1")
    assert view.status == "modified"
    report = await store.revert(OWNER, "c1", {"p1": root})

    assert report.reverted == ["app.py"]
    assert (root / "app.py").read_text() == "zero\n"


async def test_a_legacy_row_then_tagged_turns_rewind_to_after_the_legacy_one(tmp_path: Path):
    root = _project(tmp_path)
    store = await _store(tmp_path)
    await _turn(store, root, None, {"app.py": "legacy\n"})
    await _turn(store, root, "t1", {"app.py": "one\n"})
    await _turn(store, root, "t2", {"app.py": "two\n"})

    _ = await _rewind(store, root, ["t1", "t2"])
    assert (root / "app.py").read_text() == "legacy\n"
    # What the rewind put back no longer counts, so the rest of the ledger still reverts.
    _ = await store.revert(OWNER, "c1", {"p1": root})
    assert (root / "app.py").read_text() == "zero\n"


async def test_the_rewind_survives_a_lost_database_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    root = _project(tmp_path)
    store = await _store(tmp_path)
    await _turn(store, root, "t1", {"app.py": "one\n"})
    plan = await store.rewind_plan(OWNER, "c1", ["t1"], {"p1": root})

    real = Session.commit
    calls = 0

    def commit(self: Session) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OperationalError("COMMIT", {}, Exception("database is locked"))
        real(self)

    monkeypatch.setattr(Session, "commit", commit)
    done = await store.rewind_files(OWNER, "c1", ["t1"], {"p1": root}, plan.preview_token)

    assert [f.path for f in done.restore] == ["app.py"]
    assert (root / "app.py").read_text() == "zero\n"
    assert [v.reverted for v in await store.list(OWNER, "c1")] == [True]


async def test_a_file_that_cannot_be_written_is_reported_and_the_rest_go_on(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    root = _project(tmp_path)
    _ = (root / "bad.py").write_text("bad zero\n")
    store = await _store(tmp_path)
    await _turn(store, root, "t1", {"app.py": "one\n", "bad.py": "bad one\n"})
    plan = await store.rewind_plan(OWNER, "c1", ["t1"], {"p1": root})
    real = Path.write_bytes

    def write_bytes(self: Path, data: bytes) -> int:
        if self.name == "bad.py":
            raise OSError("read-only")
        return real(self, data)

    monkeypatch.setattr(Path, "write_bytes", write_bytes)
    done = await store.rewind_files(OWNER, "c1", ["t1"], {"p1": root}, plan.preview_token)

    assert [f.path for f in done.restore] == ["app.py"]
    assert [(f.path, f.reason) for f in done.failed] == [("bad.py", "read-only")]
    assert (root / "app.py").read_text() == "zero\n"
    assert {v.path: v.reverted for v in await store.list(OWNER, "c1")} == {
        "app.py": True,
        "bad.py": False,
    }


async def test_a_busy_thread_refuses_a_revert_and_a_rewind(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    patch_model_resolution(monkeypatch)
    root = _project(tmp_path)
    async with client_app() as (client, app):
        conv, _user, assistant = await _conversation_with_a_turn(client, app, root)
        app_state(app).runs.claim(conv, OWNER)

        revert = await client.post(f"/conversations/{conv}/changes/revert", json={})
        rewind = await client.post(
            f"/conversations/{conv}/changes/rewind",
            json={"messageId": assistant, "previewToken": "x"},
        )

        assert (revert.status_code, rewind.status_code) == (409, 409)
        assert (root / "app.py").read_text() == "changed\n"
