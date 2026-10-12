"""Git-style View history: the snapshot store (capture/dedup/skip/diff), the sandbox
file-collection seam, the engine capture hook, the REST surface, and delete cleanup."""

from __future__ import annotations

import hashlib

import pytest
from sqlmodel import Session, select

from core.config import Settings
from core.db import init_db, make_engine
from core.exceptions import NotFoundError
from core.vault import Vault
from models.workspace_history import WorkspaceBlob, WorkspaceSnapshot
from services.sandbox import SandboxSessionManager
from services.workspace_history import WorkspaceHistoryStore

from ._helpers import client_app, collect_sse_events, patch_model_resolution
from .conftest import egress_policy
from .vm_fakes import FakeVM

_EXCLUDES = Settings().sandbox_walk_excludes


async def _store(tmp_path) -> WorkspaceHistoryStore:
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(tmp_path / "keyfile.json")
    await vault.setup("pw")
    return WorkspaceHistoryStore(engine, vault)


def _manager(tmp_path, vault) -> SandboxSessionManager:
    return SandboxSessionManager(
        FakeVM(tmp_path / "sandbox" / "work"),
        vault,
        egress=egress_policy(tmp_path),
        data_dir=tmp_path,
        idle_ttl_s=1800.0,
        reap_interval_s=60.0,
        excludes=_EXCLUDES,
    )


# --- the store: capture, dedup, every-show-is-a-version ----------------------
async def test_capture_records_a_snapshot(tmp_path):
    store = await _store(tmp_path)
    snap = await store.capture(
        "operator", "conv-1", run_id="r1", files={"a.py": b"print(1)\n", "b.txt": b"hi"}
    )
    assert snap.stats == {"added": 2, "modified": 0, "removed": 0}
    assert snap.files_changed == 2
    assert snap.summary == "+2 ~0 -0"
    # No preview descriptor unless one is stamped (a `show(serve=…)`/auto capture).
    assert snap.preview_artifact_id is None and snap.preview_kind is None
    assert [s.id for s in await store.list("operator", "conv-1")] == [snap.id]


async def test_exact_reshow_collapses_onto_the_existing_version(tmp_path):
    store = await _store(tmp_path)
    first = await store.capture(
        "operator",
        "c",
        run_id="r1",
        files={"a.py": b"x"},
        preview_kind="image",
        preview_artifact_id="a1",
    )
    # Identical tree AND identical preview → no duplicate; the existing version returns.
    again = await store.capture(
        "operator",
        "c",
        run_id="r2",
        files={"a.py": b"x"},
        preview_kind="image",
        preview_artifact_id="a1",
    )
    assert again.id == first.id
    assert [s.id for s in await store.list("operator", "c")] == [first.id]


async def test_same_tree_new_preview_records_a_new_version(tmp_path):
    store = await _store(tmp_path)
    first = await store.capture(
        "operator",
        "c",
        run_id="r1",
        files={"a.py": b"x"},
        preview_kind="image",
        preview_artifact_id="a1",
    )
    # Same tree but a fresh preview (a different `show(file=…)`) is a real new version.
    second = await store.capture(
        "operator",
        "c",
        run_id="r2",
        files={"a.py": b"x"},
        preview_kind="html",
        preview_artifact_id="a2",
    )
    assert second.id != first.id
    assert second.stats == {"added": 0, "modified": 0, "removed": 0}
    assert [s.id for s in await store.list("operator", "c")] == [first.id, second.id]


async def test_capture_stamps_and_reads_back_the_preview_descriptor(tmp_path):
    store = await _store(tmp_path)
    snap = await store.capture(
        "operator",
        "c",
        run_id="r1",
        files={"index.html": b"<p>x</p>"},
        title="My Page",
        preview_artifact_id="a1",
        preview_kind="image",
    )
    assert snap.title == "My Page"
    assert snap.preview_artifact_id == "a1" and snap.preview_kind == "image"
    [listed] = await store.list("operator", "c")
    assert listed.preview_artifact_id == "a1" and listed.preview_kind == "image"


async def test_unchanged_file_shares_one_blob(tmp_path):
    store = await _store(tmp_path)
    await store.capture("operator", "c", run_id="r1", files={"a.py": b"same", "b.py": b"one"})
    await store.capture("operator", "c", run_id="r2", files={"a.py": b"same", "b.py": b"two"})
    with Session(store._engine) as session:
        rows = session.exec(select(WorkspaceBlob)).all()
    # a.py's content is stored once; b.py's two versions are two blobs → 3 total.
    assert len(rows) == 3
    assert all(b"same" not in r.blob_enc and b"two" not in r.blob_enc for r in rows)  # encrypted


async def test_manifest_is_encrypted_at_rest(tmp_path):
    store = await _store(tmp_path)
    await store.capture("operator", "c", run_id="r1", files={"secret_dir/private.py": b"x"})
    with Session(store._engine) as session:
        row = session.exec(select(WorkspaceSnapshot)).one()
    # The file tree (paths/names) is sealed, not stored in the clear.
    assert b"secret_dir" not in row.manifest_enc
    assert b"private.py" not in row.manifest_enc


# --- diff + file browsing ----------------------------------------------------
async def test_diff_reports_added_modified_removed(tmp_path):
    store = await _store(tmp_path)
    await store.capture(
        "operator", "c", run_id="r1", files={"keep.py": b"v1\n", "gone.py": b"bye\n"}
    )
    snap2 = await store.capture(
        "operator", "c", run_id="r2", files={"keep.py": b"v2\n", "new.py": b"hi\n"}
    )
    diffs = {d.path: d for d in await store.diff("operator", snap2.id)}
    assert diffs["keep.py"].status == "modified"
    assert "-v1" in diffs["keep.py"].diff and "+v2" in diffs["keep.py"].diff
    assert diffs["new.py"].status == "added"
    assert diffs["gone.py"].status == "removed"


async def test_files_lists_paths_with_change_status(tmp_path):
    store = await _store(tmp_path)
    await store.capture("operator", "c", run_id="r1", files={"a.py": b"1", "b.py": b"1"})
    snap2 = await store.capture(
        "operator", "c", run_id="r2", files={"a.py": b"1", "b.py": b"2", "c.py": b"new"}
    )
    statuses = {e.path: e.status for e in await store.files("operator", snap2.id)}
    assert statuses == {"a.py": "unchanged", "b.py": "modified", "c.py": "added"}


async def test_file_bytes_round_trips_and_404s_unknown(tmp_path):
    store = await _store(tmp_path)
    snap = await store.capture("operator", "c", run_id="r1", files={"a.py": b"hello"})
    assert await store.file_bytes("operator", snap.id, "a.py") == b"hello"
    with pytest.raises(NotFoundError):
        await store.file_bytes("operator", snap.id, "missing.py")


# --- delete cleanup (snapshots + orphan blobs, shared blobs kept) ------------
async def test_delete_removes_snapshots_and_only_orphan_blobs(tmp_path):
    store = await _store(tmp_path)
    await store.capture("operator", "c1", run_id="r1", files={"a.py": b"shared", "x.py": b"c1only"})
    await store.capture("operator", "c2", run_id="r2", files={"a.py": b"shared", "y.py": b"c2only"})

    await store.delete_for_conversation("operator", "c1")

    assert await store.list("operator", "c1") == []
    assert len(await store.list("operator", "c2")) == 1
    with Session(store._engine) as session:
        shas = {r.sha256 for r in session.exec(select(WorkspaceBlob)).all()}
    assert hashlib.sha256(b"shared").hexdigest() in shas  # still used by c2
    assert hashlib.sha256(b"c1only").hexdigest() not in shas  # orphaned → gone
    assert hashlib.sha256(b"c2only").hexdigest() in shas


# --- the sandbox file-collection seam ----------------------------------------
async def test_collect_text_files_skips_excluded_and_binary(tmp_path):
    vault = Vault(tmp_path / "k.json")
    await vault.setup("pw")
    session = await _manager(tmp_path, vault).acquire("conv-a")
    session.write_file("src/app.py", b"print('hi')\n")
    session.write_file("notes.txt", b"text")
    session.write_file("node_modules/dep/index.js", b"junk")  # excluded dir
    session.write_file("logo.png", b"\x89PNG\r\n\x1a\n")  # binary (invalid utf-8)
    session.write_file("data.bin", b"valid\x00utf8\x00but-binary")  # NUL: valid utf-8

    files = session.collect_text_files()

    assert set(files) == {"src/app.py", "notes.txt"}  # both binaries skipped
    assert files["notes.txt"] == b"text"


# --- REST surface ------------------------------------------------------------
async def test_snapshot_routes_list_files_content_and_diff():
    async with client_app() as (client, app):
        store = app.state.workspace_history
        await store.capture("operator", "conv-1", run_id="r1", files={"a.py": b"v1\n"})
        snap2 = await store.capture(
            "operator", "conv-1", run_id="r2", files={"a.py": b"v2\n", "b.py": b"new\n"}
        )

        listing = await client.get("/views/snapshots", params={"conversation_id": "conv-1"})
        assert listing.status_code == 200 and len(listing.json()) == 2

        files = await client.get(f"/views/snapshots/{snap2.id}/files")
        statuses = {f["path"]: f["status"] for f in files.json()}
        assert statuses == {"a.py": "modified", "b.py": "added"}

        content = await client.get(f"/views/snapshots/{snap2.id}/file", params={"path": "a.py"})
        assert content.status_code == 200 and content.content == b"v2\n"
        assert "sandbox" in content.headers["content-security-policy"]

        diff = await client.get(f"/views/snapshots/{snap2.id}/diff")
        dmap = {d["path"]: d for d in diff.json()}
        assert dmap["a.py"]["status"] == "modified" and "+v2" in dmap["a.py"]["diff"]

        assert (await client.get("/views/snapshots/nope/files")).status_code == 404


# --- cold-read integration + delete via the conversations surface ------------
async def _start_conversation(client) -> str:
    resp = await client.post("/chat", json={"prompt": "hi"})
    body = resp.json()
    await collect_sse_events(client, body["run_id"])
    return body["conversation_id"]


async def test_conversation_detail_includes_snapshots_and_delete_clears_them(monkeypatch):
    patch_model_resolution(monkeypatch, output_text="hi")
    async with client_app() as (client, app):
        conv = await _start_conversation(client)
        await app.state.workspace_history.capture(
            "operator", conv, run_id="r1", files={"a.py": b"x"}
        )

        detail = (await client.get(f"/conversations/{conv}")).json()
        assert len(detail["snapshots"]) == 1
        snap = detail["snapshots"][0]
        assert snap["files_changed"] == 1
        # An auto/no-preview capture carries a null preview descriptor.
        assert snap["preview_kind"] is None and snap["preview_artifact_id"] is None

        assert (await client.delete(f"/conversations/{conv}")).status_code == 204
        assert await app.state.workspace_history.list("operator", conv) == []
