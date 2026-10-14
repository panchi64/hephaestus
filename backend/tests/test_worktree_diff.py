"""The merge gate's contract: per-file rows, their risk verdict, and branch staleness.

Against real git, like `test_worktree.py` beside it — the rows are parsed from
``--numstat -z`` and ``--name-status -z``, and the whole value of that parsing is that it
survives what git actually emits: a rename as one record rather than two, a path with a
space in it, a deletion that is not just a file with zero lines left.

The classification assertions are the load-bearing ones. The frontend renders the verdict
and never computes it, so if a lockfile stops reading as a dependency change here, it
stops reading as one everywhere, and nothing downstream can notice.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from services.conversations import ConversationStore
from services.projects.diffstat import file_changes
from services.projects.repo import branch_for
from services.projects.worktree import WorktreeManager

from ._helpers import app_state, client_app, patch_model_resolution
from ._json import json_body, s


async def _git(cwd: Path, *args: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    _ = await proc.communicate()


async def _commit(cwd: Path, message: str) -> None:
    await _git(cwd, "add", "-A")
    await _git(cwd, "-c", "user.name=T", "-c", "user.email=t@e", "commit", "-m", message)


async def _repo(tmp_path: Path) -> Path:
    """A repository with a file of each interesting kind already committed."""
    root = tmp_path / "project"
    root.mkdir()
    _ = (root / "app.py").write_text("print('hi')\n")
    _ = (root / "uv.lock").write_text("version = 1\n")
    _ = (root / "doomed.py").write_text("unused = True\n")
    _ = (root / "moved me.py").write_text("x = 1\n")
    await _git(root, "init", "-b", "main")
    await _commit(root, "first")
    return root


def _manager(tmp_path: Path) -> WorktreeManager:
    return WorktreeManager(tmp_path / "worktrees")


async def _worktree_with_changes(tmp_path: Path) -> tuple[WorktreeManager, Path, Path]:
    """A branch holding one change of every band the merge gate separates."""
    root = await _repo(tmp_path)
    manager = _manager(tmp_path)
    state = await manager.acquire(project_id="p1", root=root, base_ref="main", conversation_id="c1")
    path = state.path
    _ = (path / "app.py").write_text("print('hi')\nprint('there')\n")
    _ = (path / "uv.lock").write_text("version = 2\n")
    _ = (path / "docker-compose.yml").write_text("services: {}\n")
    _ = (path / "README.md").write_text("# docs\n")
    _ = (path / "vite.config.ts").write_text("export default {}\n")
    (path / "tests").mkdir()
    _ = (path / "tests" / "test_app.py").write_text("def test_x(): ...\n")
    (path / "doomed.py").unlink()
    await _git(path, "mv", "moved me.py", "moved.py")
    return manager, root, path


class TestTheRows:
    async def test_every_change_comes_back_as_its_own_row(self, tmp_path: Path):
        manager, root, _path = await _worktree_with_changes(tmp_path)

        diff = await manager.diff(root, base_ref="main", conversation_id="c1", project_id="p1")

        by_path = {row.path: row for row in diff.files}
        assert by_path["app.py"].status == "modified"
        assert by_path["app.py"].insertions == 1
        assert by_path["doomed.py"].status == "deleted"
        assert by_path["docker-compose.yml"].status == "added"

    async def test_a_rename_is_a_move_and_not_a_delete_beside_an_add(self, tmp_path: Path):
        """``-M``, and a path with a space in it — the two things that break a diff
        parser that splits on whitespace and trusts one record per file."""
        manager, root, _path = await _worktree_with_changes(tmp_path)

        diff = await manager.diff(root, base_ref="main", conversation_id="c1", project_id="p1")

        moved = next(row for row in diff.files if row.path == "moved.py")
        assert moved.status == "renamed"
        assert moved.old_path == "moved me.py"
        assert not any(row.path == "moved me.py" for row in diff.files)

    async def test_the_rows_cover_the_same_files_the_shortstat_counted(self, tmp_path: Path):
        manager, root, _path = await _worktree_with_changes(tmp_path)

        diff = await manager.diff(root, base_ref="main", conversation_id="c1", project_id="p1")

        assert len(diff.files) == diff.files_changed


class TestTheVerdict:
    async def test_the_bands_the_merge_gate_separates(self, tmp_path: Path):
        manager, root, _path = await _worktree_with_changes(tmp_path)

        diff = await manager.diff(root, base_ref="main", conversation_id="c1", project_id="p1")

        bands = {row.path: (row.category, row.risk) for row in diff.files}
        assert bands["uv.lock"] == ("dependency", "high")
        assert bands["docker-compose.yml"] == ("infrastructure", "high")
        assert bands["vite.config.ts"] == ("config", "elevated")
        assert bands["app.py"] == ("code", "normal")
        assert bands["tests/test_app.py"] == ("test", "normal")
        assert bands["README.md"] == ("docs", "normal")

    async def test_a_deletion_never_sits_at_normal(self, tmp_path: Path):
        """ "Was this used?" is the question the gate exists to raise, and a file that is
        gone cannot raise it for itself."""
        manager, root, _path = await _worktree_with_changes(tmp_path)

        diff = await manager.diff(root, base_ref="main", conversation_id="c1", project_id="p1")

        deleted = next(row for row in diff.files if row.path == "doomed.py")
        assert deleted.risk == "elevated"
        assert "deleted" in deleted.reason

    async def test_the_riskiest_rows_arrive_first(self, tmp_path: Path):
        """The order is part of the verdict: a client rendering the list top to bottom is
        already triaging, and does not decide anything to do it."""
        manager, root, _path = await _worktree_with_changes(tmp_path)

        diff = await manager.diff(root, base_ref="main", conversation_id="c1", project_id="p1")

        risks = [row.risk for row in diff.files]
        assert risks == sorted(risks, key=["high", "elevated", "normal"].index)
        assert diff.files[0].category in {"dependency", "infrastructure"}

    def test_a_binary_file_says_so_rather_than_reporting_zero_changes(self):
        rows = file_changes("-\t-\tlogo.png\0", "M\0logo.png\0")

        assert rows[0].binary is True
        assert rows[0].insertions == 0
        assert rows[0].category == "asset"


class TestStaleness:
    async def test_a_branch_reports_how_far_it_has_drifted(self, tmp_path: Path):
        """Ahead of the base by the agent's work, behind it by whatever the operator
        landed meanwhile. Reviewing against a base that has moved is the failure this
        number names."""
        manager, root, _path = await _worktree_with_changes(tmp_path)
        _ = await manager.diff(root, base_ref="main", conversation_id="c1", project_id="p1")

        _ = (root / "operator.txt").write_text("their own work\n")
        await _commit(root, "operator's commit")

        diff = await manager.diff(root, base_ref="main", conversation_id="c1", project_id="p1")

        assert diff.ahead == 1
        assert diff.behind == 1
        assert diff.last_commit_at and diff.last_commit_at.startswith("20")
        # The operator's own commit is not the agent's work, and must not show up as it.
        assert not any(row.path == "operator.txt" for row in diff.files)

    async def test_a_branch_with_no_work_is_neither_ahead_nor_behind(self, tmp_path: Path):
        root = await _repo(tmp_path)
        manager = _manager(tmp_path)
        _ = await manager.acquire(project_id="p1", root=root, base_ref="main", conversation_id="c1")

        diff = await manager.diff(root, base_ref="main", conversation_id="c1", project_id="p1")

        assert (diff.ahead, diff.behind) == (0, 0)
        assert diff.files == []
        # The branch exists, so it has a tip — the base's commit, which is the honest
        # answer to "when was this last touched".
        assert diff.last_commit_at is not None

    async def test_a_base_ref_that_is_gone_stops_reporting_rather_than_failing(
        self, tmp_path: Path
    ):
        """The operator can delete or rename a branch at any time. Staleness going quiet
        is the right degrade; the diff read they are standing in front of must not 500."""
        root = await _repo(tmp_path)
        manager = _manager(tmp_path)
        _ = await manager.acquire(project_id="p1", root=root, base_ref="main", conversation_id="c1")

        ahead, behind = await manager.divergence(
            root, base_ref="no-such-ref", branch=branch_for("c1")
        )

        assert (ahead, behind) == (0, 0)


class TestTheRouteCarriesIt:
    async def test_the_branch_surface_serves_the_rows_camelcased(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The wire shape two frontend tracks build against — camelCase, per this
        surface's convention, with the verdict already on each row."""
        async with client_app() as (client, app):
            root = await _repo(tmp_path)
            project = json_body(
                await client.post("/projects", json={"name": "work", "rootPath": str(root)})
            )
            patch_model_resolution(monkeypatch)
            created = await client.post("/chat", json={"prompt": "hi", "project_id": project["id"]})
            conversation_id = s(json_body(created)["conversation_id"])
            assert await app_state(app, "conversations", ConversationStore).open_worktree(
                conversation_id
            )
            state = await app_state(app, "worktrees", WorktreeManager).acquire(
                project_id=s(project["id"]),
                root=root,
                base_ref=s(project["baseRef"]),
                conversation_id=conversation_id,
            )
            _ = (state.path / "uv.lock").write_text("version = 2\n")

            body = json_body(await client.get(f"/worktrees/{conversation_id}"))

            assert body["ahead"] == 1
            assert body["behind"] == 0
            assert body["lastCommitAt"]
            assert body["files"] == [
                {
                    "path": "uv.lock",
                    "oldPath": None,
                    "status": "modified",
                    "insertions": 1,
                    "deletions": 1,
                    "binary": False,
                    "category": "dependency",
                    "risk": "high",
                    "reason": "declares what the project depends on",
                }
            ]

    async def test_a_thread_with_no_branch_answers_with_empty_rows(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        async with client_app() as (client, app):
            root = await _repo(tmp_path)
            project = json_body(
                await client.post("/projects", json={"name": "work", "rootPath": str(root)})
            )
            patch_model_resolution(monkeypatch)
            created = await client.post("/chat", json={"prompt": "hi", "project_id": project["id"]})

            conversation_id = s(json_body(created)["conversation_id"])
            assert await app_state(app, "conversations", ConversationStore).open_worktree(
                conversation_id
            )
            body = json_body(await client.get(f"/worktrees/{conversation_id}"))

            assert body["files"] == []
            assert (body["ahead"], body["behind"]) == (0, 0)
            assert body["lastCommitAt"] is None
