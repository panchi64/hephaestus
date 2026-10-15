"""The home's files: the library's backend contract, and the containment that is ours."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic_ai.workspaces.conformance import WorkspaceBackendSuite
from pydantic_ai.workspaces.protocol import WorkspaceBackend, WorkspaceReadOnlyError

from services.home.backend import HomeBackend, Mount
from services.home.paths import HOME, SCRATCH, HomePathError, Place

API = Place("projects", "api")
WEB = Place("projects", "web")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class TestTheLibrarysContract(WorkspaceBackendSuite):
    """Run from scratch: the suite needs a writable working directory, and the home's own
    top level is not one."""

    @pytest.fixture
    def backend(self, tmp_path: Path) -> WorkspaceBackend:
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        return HomeBackend(
            [Mount(SCRATCH, scratch, writable=True)], key="t", working_dir=SCRATCH.path
        )


def _home(tmp_path: Path) -> tuple[HomeBackend, Path, Path, Path]:
    scratch, api, web = (tmp_path / name for name in ("scratch", "api", "web"))
    for root in (scratch, api, web):
        root.mkdir()
    _ = (api / "README.md").write_text("api\n")
    backend = HomeBackend(
        [
            Mount(SCRATCH, scratch, writable=True),
            Mount(API, api, writable=True),
            Mount(WEB, web, writable=False),
        ],
        key="t",
    )
    return backend, scratch, api, web


async def test_the_home_lists_its_places(tmp_path: Path) -> None:
    backend, _scratch, _api, _web = _home(tmp_path)
    assert [e.name for e in await backend.list_dir(HOME)] == [
        "scratch",
        "projects",
        "worktrees",
        "shared",
    ]
    assert [e.name for e in await backend.list_dir("~/projects")] == ["api", "web"]
    assert (await backend.stat("~/projects")).is_dir
    assert await backend.exists("~/projects/api/README.md")
    assert not await backend.exists("~/projects/nope/README.md")


async def test_a_file_lands_in_its_mounts_folder(tmp_path: Path) -> None:
    backend, scratch, api, _web = _home(tmp_path)
    await backend.write_bytes("~/scratch/notes.md", b"n")
    await backend.write_bytes("/work/alias.md", b"a")
    await backend.write_bytes("~/projects/api/src/x.py", b"x")
    assert (scratch / "notes.md").read_bytes() == b"n"
    assert (scratch / "alias.md").read_bytes() == b"a"
    assert (api / "src" / "x.py").read_bytes() == b"x"
    listed = await backend.list_dir("~/projects/api")
    project = f"{HOME}/projects/api"
    assert {e.path for e in listed} == {f"{project}/README.md", f"{project}/src"}


async def test_a_read_only_mount_reads_and_refuses_writes(tmp_path: Path) -> None:
    backend, _scratch, _api, web = _home(tmp_path)
    _ = (web / "index.html").write_text("<p>")
    assert await backend.read_bytes("~/projects/web/index.html") == b"<p>"
    with pytest.raises(WorkspaceReadOnlyError):
        await backend.write_bytes("~/projects/web/index.html", b"x")
    with pytest.raises(WorkspaceReadOnlyError):
        await backend.remove("~/projects/web/index.html")
    with pytest.raises(WorkspaceReadOnlyError):
        await backend.make_dir("~/projects/web/new")


async def test_a_symlink_out_of_its_mount_is_refused(tmp_path: Path) -> None:
    backend, scratch, api, _web = _home(tmp_path)
    secret = tmp_path / "secret.txt"
    _ = secret.write_text("operator's")
    os.symlink(secret, scratch / "leak")
    os.symlink(api, scratch / "into-api")
    with pytest.raises(PermissionError):
        _ = await backend.read_bytes("~/scratch/leak")
    with pytest.raises(PermissionError):
        await backend.write_bytes("~/scratch/leak", b"overwritten")
    # Another mount's folder is still outside this one.
    with pytest.raises(PermissionError):
        _ = await backend.read_bytes("~/scratch/into-api/README.md")
    with pytest.raises(OSError):
        _ = await backend.realpath("~/scratch/leak")
    assert secret.read_text() == "operator's"


async def test_a_symlink_inside_its_mount_resolves_to_its_virtual_path(tmp_path: Path) -> None:
    backend, _scratch, api, _web = _home(tmp_path)
    os.symlink(api / "README.md", api / "LINK.md")
    assert await backend.realpath("~/projects/api/LINK.md") == f"{HOME}/projects/api/README.md"
    assert await backend.read_bytes("~/projects/api/LINK.md") == b"api\n"


async def test_the_homes_own_folders_cannot_be_removed_or_written(tmp_path: Path) -> None:
    backend, _scratch, _api, _web = _home(tmp_path)
    with pytest.raises(ValueError):
        await backend.remove("~/projects")
    with pytest.raises(ValueError):
        await backend.remove("~/projects/api")
    with pytest.raises(IsADirectoryError):
        await backend.write_bytes("~/projects", b"x")
    with pytest.raises(IsADirectoryError):
        _ = await backend.read_bytes(HOME)


async def test_a_place_the_thread_has_no_mount_for_is_not_there(tmp_path: Path) -> None:
    backend, _scratch, _api, _web = _home(tmp_path)
    with pytest.raises(FileNotFoundError):
        _ = await backend.read_bytes("~/shared/recipes/chili.md")
    with pytest.raises(HomePathError):
        _ = await backend.read_bytes("/etc/passwd")
