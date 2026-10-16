"""A thread's mounts: its scratch, the operator's projects, and which it may write."""

from __future__ import annotations

from pathlib import Path

from core.db import init_db, make_engine
from core.vault import Vault
from services.home.leases import WriteLeases
from services.home.paths import SCRATCH, Place
from services.home.table import default_place, thread_mounts
from services.projects.store import ProjectStore
from services.settings_store import SettingsStore

OWNER = "operator"


async def _stores(tmp_path: Path) -> tuple[ProjectStore, WriteLeases]:
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(tmp_path / "keyfile.json")
    await vault.setup("pw")
    return ProjectStore(engine, vault, SettingsStore(engine)), WriteLeases(engine, ttl_s=3600)


async def test_a_thread_writes_its_scratch_and_reads_every_project(tmp_path: Path) -> None:
    projects, leases = await _stores(tmp_path)
    for name in ("API", "Web"):
        (tmp_path / name).mkdir()
        _ = await projects.create(OWNER, name, str(tmp_path / name))
    mounts = await thread_mounts(
        owner_id=OWNER,
        lease_holder="c1",
        scratch=tmp_path / "scratch",
        projects=projects,
        leases=leases,
    )
    assert [(m.place, m.writable) for m in mounts] == [
        (SCRATCH, True),
        (Place("projects", "api"), False),
        (Place("projects", "web"), False),
    ]


async def test_a_lease_or_starting_in_a_project_makes_it_writable(tmp_path: Path) -> None:
    projects, leases = await _stores(tmp_path)
    (tmp_path / "api").mkdir()
    (tmp_path / "web").mkdir()
    api = await projects.create(OWNER, "api", str(tmp_path / "api"))
    _ = await projects.create(OWNER, "web", str(tmp_path / "web"))
    await leases.lease(OWNER, "c1", Place("projects", "web"))
    mounts = await thread_mounts(
        owner_id=OWNER,
        lease_holder="c1",
        scratch=tmp_path / "scratch",
        projects=projects,
        leases=leases,
        filed_project_id=api.id,
        filed_writable=True,
    )
    assert {m.place.name: m.writable for m in mounts[1:]} == {"api": True, "web": True}
    # The lease is this conversation's alone.
    other = await thread_mounts(
        owner_id=OWNER,
        lease_holder="c2",
        scratch=tmp_path / "scratch",
        projects=projects,
        leases=leases,
    )
    assert {m.place.name: m.writable for m in other[1:]} == {"api": False, "web": False}


async def test_a_worktree_is_its_own_place_and_leaves_the_folder_alone(tmp_path: Path) -> None:
    projects, leases = await _stores(tmp_path)
    (tmp_path / "api").mkdir()
    (tmp_path / "wt").mkdir()
    api = await projects.create(OWNER, "api", str(tmp_path / "api"))
    mounts = await thread_mounts(
        owner_id=OWNER,
        lease_holder="c1",
        scratch=tmp_path / "scratch",
        projects=projects,
        leases=leases,
        filed_project_id=api.id,
        worktree_root=tmp_path / "wt",
    )
    assert [(m.place, m.root.name, m.writable) for m in mounts[1:]] == [
        (Place("projects", "api"), "api", False),
        (Place("worktrees", "api"), "wt", True),
    ]
    assert default_place(mounts, "api") == Place("worktrees", "api")
    assert default_place(mounts[:2], "api") == Place("projects", "api")
    assert default_place(mounts, None) == SCRATCH


async def test_an_archived_project_is_not_in_the_home(tmp_path: Path) -> None:
    projects, leases = await _stores(tmp_path)
    (tmp_path / "old").mkdir()
    old = await projects.create(OWNER, "old", str(tmp_path / "old"))
    _ = await projects.update(OWNER, old.id, archived=True)
    mounts = await thread_mounts(
        owner_id=OWNER,
        lease_holder="c1",
        scratch=tmp_path / "scratch",
        projects=projects,
        leases=leases,
    )
    assert [m.place for m in mounts] == [SCRATCH]


async def test_a_released_lease_stops_the_writes(tmp_path: Path) -> None:
    _projects, leases = await _stores(tmp_path)
    web = Place("projects", "web")
    await leases.lease(OWNER, "c1", web)
    assert await leases.held(OWNER, "c1") == {"projects/web"}
    await leases.release(OWNER, "c1", web)
    assert await leases.held(OWNER, "c1") == set()
