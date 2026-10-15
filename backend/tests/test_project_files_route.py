"""The `@` picker's listing: which filesystem answers, and what it will not do.

The subtle one is the root. A worktree that exists on disk but has no entry in the
manager's in-memory holder map — every worktree, after a backend restart — must still be
recognised as the thread's, or the picker quietly lists the operator's own checkout while
the agent reads the branch. The operator would pick a path from one tree and the agent
would resolve it in another, and nothing would say so.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import httpx
import pytest

from routes.deps import app_state
from services.projects.listing import list_files
from services.projects.tree import worktree_root
from services.sandbox.base import contained_file

from ._helpers import client_app
from ._json import arr, json_body, obj, s


def _git(cwd: Path, *args: str) -> None:
    _ = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    _ = (root / "src" / "app.tsx").write_text("x")
    _ = (root / "src" / "helper.ts").write_text("x")
    _ = (root / "README.md").write_text("x")
    (root / "node_modules" / "junk").mkdir(parents=True)
    _ = (root / "node_modules" / "junk" / "a.js").write_text("x")
    _ = (root / ".gitignore").write_text("node_modules/\n")
    _git(root, "init", "-q")
    return root


class TestListing:
    async def test_gitignored_directories_never_appear(self, repo: Path):
        listing = await list_files(repo)
        # Not a filter afterwards — `git ls-files --exclude-standard` means the walk
        # never enters `node_modules` at all.
        assert not [e for e in listing.entries if "node_modules" in e.path]
        assert {"src/app.tsx", "README.md"} <= {e.path for e in listing.entries}

    async def test_untracked_files_are_offered(self, repo: Path):
        # Nothing is committed in this fixture, so every file is untracked — which is
        # exactly the state a fresh project is in, and a picker that showed nothing
        # there would be useless on the first day of a repository.
        assert {e.path for e in (await list_files(repo)).entries} >= {"README.md"}

    async def test_a_directory_that_is_not_a_repository_still_lists(self, tmp_path: Path):
        plain = tmp_path / "plain"
        (plain / "deep").mkdir(parents=True)
        _ = (plain / "deep" / "note.md").write_text("x")
        (plain / "node_modules").mkdir()
        _ = (plain / "node_modules" / "x.js").write_text("x")
        listing = await list_files(plain)
        assert {e.path for e in listing.entries} == {"deep/note.md"}

    async def test_a_name_match_outranks_a_directory_match(self, repo: Path):
        # `apps/` contains the query in its *path* only; `src/app.tsx` has it at the
        # start of its name. Laid out so the alphabetical order of the paths disagrees
        # with the ranking — `apps/...` sorts before `src/...` — and only the buckets
        # give the right answer.
        (repo / "apps").mkdir()
        _ = (repo / "apps" / "zebra.ts").write_text("x")
        paths = [e.path for e in (await list_files(repo, query="app")).entries]
        assert paths.index("src/app.tsx") < paths.index("apps/zebra.ts")

    async def test_a_name_prefix_outranks_a_name_substring(self, repo: Path):
        _ = (repo / "myapp.ts").write_text("x")
        paths = [e.path for e in (await list_files(repo, query="app")).entries]
        assert paths.index("src/app.tsx") < paths.index("myapp.ts")

    async def test_the_limit_reports_truncation(self, repo: Path):
        listing = await list_files(repo, limit=1)
        assert len(listing.entries) == 1
        assert listing.truncated is True


class TestRootResolution:
    async def test_a_missing_worktree_falls_back_to_the_project(self, repo: Path, tmp_path: Path):
        root, is_worktree = await worktree_root(tmp_path / "nope", "c1", repo)
        assert (root, is_worktree) == (repo, False)

    async def test_a_worktree_on_this_thread_branch_is_used(self, repo: Path, tmp_path: Path):
        _git(repo, "add", "-A")
        _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
        tree = tmp_path / "wt"
        _git(repo, "worktree", "add", "-q", "-b", "heph/c1", str(tree))

        # The holder map is never consulted — nothing has acquired anything here, which
        # is precisely the state every worktree is in after a backend restart.
        root, is_worktree = await worktree_root(tree, "c1", repo)
        assert (root, is_worktree) == (tree, True)

    async def test_a_worktree_on_another_thread_branch_is_not_ours(
        self, repo: Path, tmp_path: Path
    ):
        _git(repo, "add", "-A")
        _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
        tree = tmp_path / "wt"
        _git(repo, "worktree", "add", "-q", "-b", "heph/c1", str(tree))
        # One worktree serves a project, so an existing directory on someone else's
        # branch is not this thread's files.
        root, is_worktree = await worktree_root(tree, "c2", repo)
        assert (root, is_worktree) == (repo, False)


class TestContainment:
    def test_a_path_inside_the_root_resolves(self, repo: Path):
        assert contained_file(repo, "src/app.tsx") == (repo / "src" / "app.tsx").resolve()

    @pytest.mark.parametrize(
        "relative",
        ["../outside.txt", "/etc/passwd", "src/../../outside.txt", "", "a\0b"],
    )
    def test_a_path_leaving_the_root_does_not(self, repo: Path, relative: str):
        assert contained_file(repo, relative) is None

    def test_a_symlink_pointing_out_does_not(self, repo: Path, tmp_path: Path):
        secret = tmp_path / "secret.txt"
        _ = secret.write_text("x")
        # Resolved on both sides, so a link out is caught rather than merely a `..`
        # spelled in the string.
        (repo / "link.txt").symlink_to(secret)
        assert contained_file(repo, "link.txt") is None

    def test_a_directory_is_not_a_file(self, repo: Path):
        assert contained_file(repo, "src") is None


class TestRoute:
    async def test_it_answers_camel_case_and_names_its_root(self, repo: Path):
        async with client_app() as (client, _app):
            created = await client.post("/projects/ensure", json={"rootPath": str(repo)})
            pid = json_body(created)["id"]
            body = json_body(await client.get(f"/projects/{pid}/files?query=app"))
        assert body["root"] == "project"
        assert obj(arr(body["entries"])[0])["path"] == "src/app.tsx"
        assert body["truncated"] is False

    async def test_an_unknown_project_is_a_404(self):
        async with client_app() as (client, _app):
            assert (await client.get("/projects/nope/files")).status_code == 404

    async def test_listing_never_creates_a_worktree(self, repo: Path):
        async with client_app() as (client, app):
            created = await client.post("/projects/ensure", json={"rootPath": str(repo)})
            pid = s(json_body(created)["id"])
            _ = await client.get(f"/projects/{pid}/files?conversation_id=c1")
            # Acquiring the project's single checkout as a side effect of typing a
            # character would take it from whatever else holds it.
            worktrees = app_state(app).worktrees
            assert worktrees.holder(pid) is None
            assert not worktrees.path_for(pid).exists()


class TestReadRoute:
    """Reading one of the files the listing just offered.

    The browsable tree is why this exists: a path can now be asked for directly rather
    than only picked out of a listing that already honours `.gitignore`, so the refusals
    are the interesting half.
    """

    async def _project(self, client: httpx.AsyncClient, repo: Path) -> str:
        created = await client.post("/projects/ensure", json={"rootPath": str(repo)})
        return s(json_body(created)["id"])

    async def test_it_serves_a_files_bytes(self, repo: Path):
        _ = (repo / "src" / "app.tsx").write_text("export const x = 1;\n")
        async with client_app() as (client, _app):
            pid = await self._project(client, repo)
            res = await client.get(f"/projects/{pid}/file?path=src/app.tsx")
        assert res.status_code == 200
        assert res.text == "export const x = 1;\n"
        # Inert, like a View's bytes: a file out of a repository must not be able to run
        # anything in the operator's own origin.
        assert res.headers["x-content-type-options"] == "nosniff"
        assert "sandbox" in res.headers["content-security-policy"]
        assert res.headers["x-content-truncated"] == "false"

    @pytest.mark.parametrize(
        "relative",
        ["../outside.txt", "/etc/passwd", "src/../../outside.txt", "src", "gone.txt"],
    )
    async def test_every_unreadable_path_is_the_same_404(self, repo: Path, relative: str):
        # One answer to four questions on purpose. Saying which of "outside the tree",
        # "a directory" and "not there" a path was would describe the filesystem around
        # something the operator is not being allowed to read.
        async with client_app() as (client, _app):
            pid = await self._project(client, repo)
            res = await client.get(f"/projects/{pid}/file", params={"path": relative})
        assert res.status_code == 404

    async def test_a_symlink_out_of_the_tree_is_refused(self, repo: Path, tmp_path: Path):
        secret = tmp_path / "secret.txt"
        _ = secret.write_text("the operator's own business")
        (repo / "link.txt").symlink_to(secret)
        async with client_app() as (client, _app):
            pid = await self._project(client, repo)
            res = await client.get(f"/projects/{pid}/file?path=link.txt")
        assert res.status_code == 404

    async def test_an_oversized_file_is_cut_and_says_so(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The ceiling has to be *detectable*, not inferred from a length that happens to
        # equal it — hence the header rather than the client comparing sizes.
        monkeypatch.setattr("routes.projects._FILE_MAX_BYTES", 8)
        _ = (repo / "big.txt").write_text("0123456789")
        async with client_app() as (client, _app):
            pid = await self._project(client, repo)
            res = await client.get(f"/projects/{pid}/file?path=big.txt")
        assert res.status_code == 200
        assert res.text == "01234567"
        assert res.headers["x-content-truncated"] == "true"

    async def test_a_file_exactly_at_the_ceiling_is_not_called_truncated(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr("routes.projects._FILE_MAX_BYTES", 10)
        _ = (repo / "exact.txt").write_text("0123456789")
        async with client_app() as (client, _app):
            pid = await self._project(client, repo)
            res = await client.get(f"/projects/{pid}/file?path=exact.txt")
        assert res.text == "0123456789"
        assert res.headers["x-content-truncated"] == "false"

    async def test_reading_never_creates_a_worktree(self, repo: Path):
        async with client_app() as (client, app):
            pid = await self._project(client, repo)
            _ = await client.get(f"/projects/{pid}/file?path=README.md&conversation_id=c1")
            # Same rule the listing keeps, and for the same reason: opening a file must
            # not take the project's single checkout from whatever holds it.
            worktrees = app_state(app).worktrees
            assert worktrees.holder(pid) is None
            assert not worktrees.path_for(pid).exists()

    async def test_an_unknown_project_is_a_404(self):
        async with client_app() as (client, _app):
            assert (await client.get("/projects/nope/file?path=README.md")).status_code == 404
