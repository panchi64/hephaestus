"""What a thread changes in the operator's own project folders, and putting it back."""

from __future__ import annotations

from pathlib import Path

from core.db import init_db, make_engine
from core.vault import Vault
from routes.deps import app_state
from services.home.backend import Mount
from services.home.changes import ChangeLog, ThreadChanges
from services.home.paths import SCRATCH, Place

from ._helpers import client_app
from ._json import arr, json_body, obj, s

OWNER = "operator"


async def _store(tmp_path: Path, *, limit: int = 1024) -> ThreadChanges:
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(tmp_path / "keyfile.json")
    await vault.setup("pw")
    return ThreadChanges(engine, vault, max_kept_bytes=limit)


async def _write(store: ThreadChanges, root: Path, rel: str, content: str | None) -> None:
    await store.before(OWNER, "c1", "p1", root, [rel])
    target = root / rel
    if content is None:
        target.unlink()
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        _ = target.write_text(content)
    await store.after(OWNER, "c1", "p1", root, [rel])


async def test_a_thread_can_put_back_what_it_changed(tmp_path: Path):
    root = tmp_path / "project"
    root.mkdir()
    _ = (root / "app.py").write_text("original\n")
    _ = (root / "old.txt").write_text("keep me\n")
    store = await _store(tmp_path)

    await _write(store, root, "app.py", "first\n")
    await _write(store, root, "app.py", "second\n")
    await _write(store, root, "src/new.py", "made\n")
    await _write(store, root, "old.txt", None)

    listed = {view.path: view.status for view in await store.list(OWNER, "c1")}
    assert listed == {"app.py": "modified", "src/new.py": "added", "old.txt": "deleted"}

    report = await store.revert(OWNER, "c1", {"p1": root})
    assert sorted(report.reverted) == ["app.py", "old.txt", "src/new.py"]
    # As it was before the first change, not before the last.
    assert (root / "app.py").read_text() == "original\n"
    assert (root / "old.txt").read_text() == "keep me\n"
    assert not (root / "src/new.py").exists()
    assert all(view.reverted for view in await store.list(OWNER, "c1"))


async def test_a_file_changed_since_is_left_alone(tmp_path: Path):
    root = tmp_path / "project"
    root.mkdir()
    _ = (root / "app.py").write_text("original\n")
    store = await _store(tmp_path)
    await _write(store, root, "app.py", "agent\n")
    _ = (root / "app.py").write_text("the operator's own edit\n")

    report = await store.revert(OWNER, "c1", {"p1": root})
    assert report.reverted == []
    assert report.skipped == [("app.py", "it has changed since this conversation last wrote it")]
    assert (root / "app.py").read_text() == "the operator's own edit\n"


async def test_a_file_too_large_to_keep_is_listed_but_not_put_back(tmp_path: Path):
    root = tmp_path / "project"
    root.mkdir()
    _ = (root / "big.bin").write_text("x" * 2048)
    store = await _store(tmp_path, limit=1024)
    await _write(store, root, "big.bin", "smaller")

    [view] = await store.list(OWNER, "c1")
    assert (view.status, view.revertible) == ("modified", False)
    report = await store.revert(OWNER, "c1", {"p1": root})
    assert report.skipped == [("big.bin", "it was too large to keep a copy of")]


async def test_a_change_after_a_revert_is_recorded_afresh(tmp_path: Path):
    root = tmp_path / "project"
    root.mkdir()
    _ = (root / "app.py").write_text("original\n")
    store = await _store(tmp_path)
    await _write(store, root, "app.py", "one\n")
    _ = await store.revert(OWNER, "c1", {"p1": root})
    await _write(store, root, "app.py", "two\n")

    [view] = await store.list(OWNER, "c1")
    assert not view.reverted
    _ = await store.revert(OWNER, "c1", {"p1": root})
    assert (root / "app.py").read_text() == "original\n"


async def test_only_the_operators_own_folder_is_recorded(tmp_path: Path):
    root = tmp_path / "project"
    root.mkdir()
    store = await _store(tmp_path)
    log = ChangeLog(store, owner_id=OWNER, conversation_id="c1")
    project = Mount(Place("projects", "api"), root, writable=True, project_id="p1")
    for unrecorded in (
        Mount(SCRATCH, root, writable=True),
        Mount(Place("worktrees", "api"), root, writable=True, project_id="p1"),
        Mount(Place("projects", "api"), root, writable=False, project_id="p1"),
    ):
        assert not log.records(unrecorded)
    assert log.records(project)
    await log.before(project, ["a.txt"])
    _ = (root / "a.txt").write_text("a")
    await log.after(project, ["a.txt"])
    assert [view.path for view in await store.list(OWNER, "c1")] == ["a.txt"]


async def test_the_routes_list_diff_and_revert(tmp_path: Path):
    root = tmp_path / "work"
    root.mkdir()
    _ = (root / "app.py").write_text("original\n")
    async with client_app() as (client, app):
        project = json_body(
            await client.post("/projects", json={"name": "work", "rootPath": str(root)})
        )
        project_id = s(project["id"])
        conversation_id = await app_state(app).conversations.create_conversation(
            OWNER, project_id=project_id
        )
        store = app_state(app).capabilities.get(ThreadChanges)
        await store.before(OWNER, conversation_id, project_id, root, ["app.py"])
        _ = (root / "app.py").write_text("changed\n")
        await store.after(OWNER, conversation_id, project_id, root, ["app.py"])

        listed = json_body(await client.get(f"/conversations/{conversation_id}/changes"))
        [group] = arr(listed["projects"])
        assert obj(group)["homePath"] == "~/projects/work"
        [file] = arr(obj(group)["files"])
        assert (obj(file)["path"], obj(file)["status"]) == ("app.py", "modified")

        diff = json_body(
            await client.get(f"/conversations/{conversation_id}/changes/{obj(file)['id']}/diff")
        )
        patch = s(diff["patch"])
        assert "-original" in patch and "+changed" in patch

        reverted = json_body(
            await client.post(f"/conversations/{conversation_id}/changes/revert", json={})
        )
        assert arr(reverted["reverted"]) == ["app.py"]
        assert (root / "app.py").read_text() == "original\n"
