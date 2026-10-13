"""Projects: the catalog, the active selection, and the one scope rule.

The scope rule is the reason this feature is safe to introduce over an existing
database, so it is what these tests are mostly about:

    visible   = unfiled  ∪  the active project
    invisible = every other project

The load-bearing assertion is that a row created *before* projects existed
(``project_id is None``) stays visible once a project is activated. Get that wrong and
activating the first project blanks the operator's entire history — which is exactly the
kind of failure that looks like a working feature in every other test.
"""

from __future__ import annotations

import pytest
from sqlmodel import Session, select

from core.db import in_session, init_db, make_engine
from core.exceptions import InvalidInputError, NotFoundError
from core.vault import Vault, VaultLocked
from models.conversation import Conversation
from models.corpus import CorpusSource
from models.task import ScheduledTask
from services.projects import ProjectStore, project_clause, visible_project_ids
from services.projects.store import SCOPED_MODELS
from services.settings_store import SettingsStore
from tests._helpers import client_app


async def _store(tmp_path) -> ProjectStore:
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(tmp_path / "keyfile.json")
    await vault.setup("pw")
    return ProjectStore(engine, vault, SettingsStore(engine))


class TestScopeRule:
    def test_unfiled_is_visible_with_no_active_project(self):
        assert visible_project_ids(None) == (None,)

    def test_active_project_adds_to_unfiled_never_replaces_it(self):
        # The whole safety argument: None (unfiled) survives activation.
        assert visible_project_ids("proj-a") == (None, "proj-a")

    def test_another_project_is_never_visible(self):
        assert "proj-b" not in visible_project_ids("proj-a")


class TestCatalog:
    async def test_create_seals_the_path_and_probes_the_repo(self, tmp_path):
        store = await _store(tmp_path)
        work = tmp_path / "work"
        work.mkdir()

        view = await store.create("operator", "Work", str(work))

        assert view.root_path == str(work.resolve())
        assert view.probe.exists is True
        # A plain directory is not a repo, and `uncommitted_changes` stays None rather
        # than 0 — "not a repo" and "a clean repo" are different answers.
        assert view.probe.is_git_repo is False
        assert view.probe.uncommitted_changes is None

    async def test_name_defaults_to_the_directory_name(self, tmp_path):
        store = await _store(tmp_path)
        work = tmp_path / "hephaestus"
        work.mkdir()
        view = await store.create("operator", "  ", str(work))
        assert view.name == "hephaestus"

    async def test_relative_path_is_refused(self, tmp_path):
        store = await _store(tmp_path)
        # A relative path would resolve against the *server's* cwd, never what the
        # operator meant, so it is rejected rather than silently reinterpreted.
        with pytest.raises(InvalidInputError):
            await store.create("operator", "x", "relative/dir")

    async def test_missing_directory_is_refused(self, tmp_path):
        store = await _store(tmp_path)
        with pytest.raises(InvalidInputError):
            await store.create("operator", "x", str(tmp_path / "nope"))

    async def test_another_owners_project_is_not_found(self, tmp_path):
        store = await _store(tmp_path)
        work = tmp_path / "work"
        work.mkdir()
        view = await store.create("operator", "Work", str(work))
        with pytest.raises(NotFoundError):
            await store.get("someone-else", view.id)


class TestEnsureForPath:
    """Pointing at a directory *is* filing it, and pointing twice files it once.

    The idempotence is not tidiness: a second row for the same directory would cut a
    second set of worktrees from one repository, and the operator would have two projects
    that disagree about the same tree.
    """

    async def test_creates_a_project_named_after_the_directory(self, tmp_path):
        store = await _store(tmp_path)
        work = tmp_path / "hephaestus"
        work.mkdir()

        view = await store.ensure_for_path("operator", str(work))

        assert view.name == "hephaestus"
        assert view.root_path == str(work.resolve())
        # Never establishes a repository — that stays the separately confirmed act, and
        # the view is what tells the caller one is still needed.
        assert view.git_initialized is False
        assert view.probe.is_git_repo is False

    async def test_the_same_directory_returns_the_same_project(self, tmp_path):
        store = await _store(tmp_path)
        work = tmp_path / "work"
        work.mkdir()

        first = await store.ensure_for_path("operator", str(work))
        # A trailing slash is the same directory; the match is on the resolved path, so
        # it must not produce a second row.
        second = await store.ensure_for_path("operator", f"{work}/")

        assert second.id == first.id
        assert len(await store.list("operator")) == 1

    async def test_it_matches_a_project_that_was_created_by_hand(self, tmp_path):
        store = await _store(tmp_path)
        work = tmp_path / "work"
        work.mkdir()
        named = await store.create("operator", "Client work", str(work))

        found = await store.ensure_for_path("operator", str(work))

        # The operator's own name survives — this adopts the existing project rather
        # than renaming it to the directory.
        assert found.id == named.id
        assert found.name == "Client work"

    async def test_an_archived_project_comes_back_unarchived(self, tmp_path):
        store = await _store(tmp_path)
        work = tmp_path / "work"
        work.mkdir()
        stale = await store.create("operator", "Work", str(work))
        await store.update("operator", stale.id, archived=True)

        revived = await store.ensure_for_path("operator", str(work))

        # Returning it archived would leave a session in a project the listing hides.
        assert revived.id == stale.id
        assert revived.archived is False

    async def test_another_owners_project_is_not_adopted(self, tmp_path):
        store = await _store(tmp_path)
        work = tmp_path / "work"
        work.mkdir()
        theirs = await store.create("someone-else", "Theirs", str(work))

        mine = await store.ensure_for_path("operator", str(work))

        assert mine.id != theirs.id

    async def test_a_missing_directory_is_still_refused(self, tmp_path):
        store = await _store(tmp_path)
        # Removing the ceremony does not remove the check: a path that is not a
        # directory cannot be worked in, and inventing a project for it would fail
        # later, further from the operator's action.
        with pytest.raises(InvalidInputError):
            await store.ensure_for_path("operator", str(tmp_path / "nope"))

    async def test_workspace_names_are_basenames_and_never_paths(self, tmp_path):
        store = await _store(tmp_path)
        work = tmp_path / "acme-api"
        work.mkdir()
        view = await store.ensure_for_path("operator", str(work))

        names = await store.workspace_names("operator")

        # The sidebar groups on this and is permanently on screen — the directory the
        # operator works in must not spell out where it lives.
        assert names == {view.id: "acme-api"}
        assert str(tmp_path) not in names[view.id]


class TestWorkspaceRootsAreMemoised:
    """The conversation rail re-reads this on every refresh — every couple of seconds
    while anything is running — so it must not be a table scan plus an AEAD decrypt per
    project each time, and it must still be *right* the moment a project is written."""

    async def test_a_repeat_read_costs_no_query_and_no_decrypt(self, tmp_path):
        store = await _store(tmp_path)
        (tmp_path / "acme").mkdir()
        await store.ensure_for_path("operator", str(tmp_path / "acme"))
        first = await store.workspace_roots("operator")

        def explode(*_args, **_kwargs):
            raise AssertionError("workspace_roots re-read the catalog on a warm call")

        store._vault.decrypt_str = explode  # type: ignore[method-assign]
        assert await store.workspace_roots("operator") == first
        assert await store.workspace_names("operator") == {pid: "acme" for pid in first}

    async def test_a_caller_cannot_edit_the_memo_through_what_it_was_handed(self, tmp_path):
        store = await _store(tmp_path)
        (tmp_path / "acme").mkdir()
        await store.ensure_for_path("operator", str(tmp_path / "acme"))

        (await store.workspace_roots("operator")).clear()

        assert await store.workspace_roots("operator") != {}

    async def test_a_new_project_shows_up_without_waiting_for_anything(self, tmp_path):
        store = await _store(tmp_path)
        (tmp_path / "acme").mkdir()
        (tmp_path / "beta").mkdir()
        await store.ensure_for_path("operator", str(tmp_path / "acme"))
        await store.workspace_roots("operator")  # warm the memo

        beta = await store.ensure_for_path("operator", str(tmp_path / "beta"))

        assert beta.id in await store.workspace_roots("operator")

    async def test_a_rename_and_a_delete_are_both_visible_immediately(self, tmp_path):
        store = await _store(tmp_path)
        (tmp_path / "acme").mkdir()
        acme = await store.ensure_for_path("operator", str(tmp_path / "acme"))
        await store.workspace_roots("operator")

        await store.update("operator", acme.id, archived=True)
        assert acme.id in await store.workspace_roots("operator")  # archived ≠ gone

        await store.delete("operator", acme.id)
        assert await store.workspace_roots("operator") == {}

    async def test_activating_a_project_reorders_the_memo_it_invalidates(self, tmp_path):
        # Insertion order is the answer, not decoration: opening a path the model named
        # searches these directories in order, and the one worked in most recently is the
        # one meant. A memo that survived `activate` would search yesterday's project.
        store = await _store(tmp_path)
        (tmp_path / "acme").mkdir()
        (tmp_path / "beta").mkdir()
        acme = await store.ensure_for_path("operator", str(tmp_path / "acme"))
        beta = await store.ensure_for_path("operator", str(tmp_path / "beta"))
        assert list(await store.workspace_roots("operator")) == [beta.id, acme.id]

        await store.activate("operator", acme.id)

        assert list(await store.workspace_roots("operator")) == [acme.id, beta.id]

    async def test_locking_the_vault_drops_the_decrypted_paths(self, tmp_path):
        # The path names the operator's clients, which is why it is sealed at rest. A
        # memo that outlived the key would keep answering with it.
        store = await _store(tmp_path)
        (tmp_path / "acme").mkdir()
        await store.ensure_for_path("operator", str(tmp_path / "acme"))
        await store.workspace_roots("operator")

        store._vault.lock()

        with pytest.raises(VaultLocked):
            await store.workspace_roots("operator")


class TestDeletingAProject:
    """Deleting a project deletes a **label**, not the work filed under it."""

    async def test_every_scoped_model_is_listed(self):
        # The unfiling below walks `SCOPED_MODELS` by hand — there is no shared base
        # class to derive it from. Another scoped entity that forgets to join the tuple
        # would have its rows orphaned on the next project delete, silently and
        # permanently, so the list is checked against the live schema instead.
        from sqlmodel import SQLModel

        scoped = {
            cls
            for cls in SQLModel.__subclasses__()
            if getattr(cls, "__tablename__", None) and "project_id" in cls.model_fields
        }
        assert scoped == set(SCOPED_MODELS)

    async def test_its_rows_are_unfiled_rather_than_orphaned(self, tmp_path):
        store = await _store(tmp_path)
        work = tmp_path / "work"
        work.mkdir()
        project = await store.create("operator", "Work", str(work))

        def seed(session: Session) -> None:
            session.add(Conversation(id="c-1", owner_id="operator", project_id=project.id))
            session.add(
                ScheduledTask(
                    id="t-1",
                    owner_id="operator",
                    project_id=project.id,
                    kind="agent",
                    title_enc="t",
                    prompt_enc="p",
                    schedule_type="run_at",
                    output="notification",
                )
            )
            session.commit()

        await in_session(store._db, seed)  # noqa: SLF001 — seeding the store's own engine
        await store.delete("operator", project.id)

        def read(session: Session) -> list[str | None]:
            return [
                session.get(Conversation, "c-1").project_id,  # type: ignore[union-attr]
                session.get(ScheduledTask, "t-1").project_id,  # type: ignore[union-attr]
            ]

        # A deleted id can never be active again, so rows still pointing at it would
        # drop out of `visible = unfiled ∪ active` forever with no way back from the UI.
        assert await in_session(store._db, read) == [None, None]  # noqa: SLF001


class TestActiveSelection:
    async def test_nothing_is_active_by_default(self, tmp_path):
        store = await _store(tmp_path)
        assert await store.active_id("operator") is None

    async def test_activate_then_deactivate_round_trips(self, tmp_path):
        store = await _store(tmp_path)
        work = tmp_path / "work"
        work.mkdir()
        view = await store.create("operator", "Work", str(work))

        await store.activate("operator", view.id)
        assert await store.active_id("operator") == view.id

        await store.activate("operator", None)
        assert await store.active_id("operator") is None

    async def test_activating_an_unknown_project_is_refused(self, tmp_path):
        store = await _store(tmp_path)
        # Refused rather than stored, so the selection can never dangle.
        with pytest.raises(NotFoundError):
            await store.activate("operator", "no-such-project")
        assert await store.active_id("operator") is None

    async def test_deleting_the_active_project_clears_the_selection(self, tmp_path):
        store = await _store(tmp_path)
        work = tmp_path / "work"
        work.mkdir()
        view = await store.create("operator", "Work", str(work))
        await store.activate("operator", view.id)

        await store.delete("operator", view.id)

        assert await store.active_id("operator") is None


class TestProjectClause:
    """The SQL half of the rule, over real rows.

    ``project_clause`` exists because ``column.in_(visible)`` reads as correct and is
    not: SQL ``IN`` never matches NULL, so it silently drops every unfiled row. These
    assert the behavior that mistake would break.
    """

    @staticmethod
    def _seeded():
        engine = make_engine("sqlite:///:memory:")
        init_db(engine)
        with Session(engine) as session:
            session.add(Conversation(owner_id="operator", project_id=None))  # pre-project
            session.add(Conversation(owner_id="operator", project_id="proj-a"))
            session.add(Conversation(owner_id="operator", project_id="proj-b"))
            session.commit()
        return engine

    @staticmethod
    def _ids(engine, visible) -> list[str]:
        with Session(engine) as session:
            query = select(Conversation)
            clause = project_clause(Conversation.project_id, visible)
            if clause is not None:
                query = query.where(clause)
            return sorted(str(c.project_id) for c in session.exec(query).all())

    def test_unfiled_rows_survive_activating_a_project(self):
        # THE assertion. A pre-projects conversation must not vanish the moment the
        # operator opens their first project.
        assert self._ids(self._seeded(), visible_project_ids("proj-a")) == ["None", "proj-a"]

    def test_another_project_is_excluded(self):
        assert "proj-b" not in self._ids(self._seeded(), visible_project_ids("proj-a"))

    def test_no_active_project_shows_only_unfiled(self):
        assert self._ids(self._seeded(), visible_project_ids(None)) == ["None"]

    def test_none_means_no_filter_at_all(self):
        # The ALL PROJECTS scope — every row, filed or not.
        assert self._ids(self._seeded(), None) == ["None", "proj-a", "proj-b"]


class TestRoutes:
    async def test_feature_wires_up_and_round_trips(self, tmp_path):
        async with client_app() as (client, app):
            assert hasattr(app.state, "projects")

            work = tmp_path / "proj"
            work.mkdir()
            created = await client.post("/projects", json={"name": "P", "rootPath": str(work)})
            assert created.status_code == 201, created.text
            pid = created.json()["id"]

            listing = (await client.get("/projects")).json()
            assert [p["id"] for p in listing["projects"]] == [pid]
            assert listing["activeId"] is None

            activated = (await client.post(f"/projects/{pid}/activate")).json()
            assert activated["activeId"] == pid

            assert (await client.post("/projects/deactivate")).json()["activeId"] is None

    async def test_ensure_files_a_directory_and_is_idempotent(self, tmp_path):
        async with client_app() as (client, _app):
            work = tmp_path / "acme-api"
            work.mkdir()

            first = await client.post("/projects/ensure", json={"rootPath": str(work)})
            assert first.status_code == 200, first.text
            assert first.json()["name"] == "acme-api"

            second = await client.post("/projects/ensure", json={"rootPath": str(work)})
            assert second.json()["id"] == first.json()["id"]
            assert len((await client.get("/projects")).json()["projects"]) == 1

    async def test_ensure_is_not_swallowed_by_the_id_route(self, tmp_path):
        async with client_app() as (client, _app):
            # `/projects/{project_id}` is a GET and `/projects/ensure` a POST, but the
            # two share a path shape — declaration order is what keeps `ensure` from
            # being read as an id, and this is what would catch a reorder.
            resp = await client.post("/projects/ensure", json={"rootPath": str(tmp_path / "no")})
            assert resp.status_code == 422, resp.text

    async def test_a_worktree_thread_carries_its_workspace_into_the_listing(self, tmp_path):
        async with client_app() as (client, app):
            work = tmp_path / "acme-api"
            work.mkdir()
            ensured = await client.post("/projects/ensure", json={"rootPath": str(work)})
            pid = ensured.json()["id"]

            def seed(session: Session) -> None:
                session.add(
                    Conversation(
                        id="c-code", owner_id="operator", project_id=pid, workspace_kind="worktree"
                    )
                )
                session.commit()

            await in_session(app.state.db_engine, seed)

            listing = await client.get("/conversations", headers={"X-Heph-Project": "all"})
            row = next(c for c in listing.json() if c["id"] == "c-code")
            # The rail groups on these two and nothing else — the workspace kind picks the section,
            # the workspace names it, and the path stays on the server.
            assert row["workspace_kind"] == "worktree"
            assert row["workspace"] == "acme-api"

    async def test_the_project_toolset_is_in_the_catalog(self):
        async with client_app() as (_client, app):
            # Registered as an ordinary category, so it appears in the operator's
            # tool settings and can be toggled like every other tool.
            assert "project" in app.state.tool_categories


class TestScopedSurfaces:
    """A *surface* actually applies the scope rule, not just the helper in isolation.

    Seeds rows straight into the booted app's engine rather than driving real chat
    turns: what is under test is the filtering, and a turn would drag a model into it.
    """

    @staticmethod
    async def _seed(app) -> None:
        # Ids are fixed so the assertions can name *which* rows came back, not just how
        # many — a count would pass while the wrong project's threads were returned.
        # Written through `in_session` like every other writer: a booted app's in-memory
        # engine shares one connection, and its drainers are already using it.
        def work(session: Session) -> None:
            session.add(Conversation(id="c-unfiled", owner_id="operator", project_id=None))
            session.add(Conversation(id="c-a", owner_id="operator", project_id="proj-a"))
            session.add(Conversation(id="c-b", owner_id="operator", project_id="proj-b"))
            session.commit()

        await in_session(app.state.db_engine, work)

    @staticmethod
    async def _listed(client, header: str | None) -> list[str]:
        headers = {"X-Heph-Project": header} if header is not None else {}
        resp = await client.get("/conversations", headers=headers)
        assert resp.status_code == 200, resp.text
        return sorted(c["id"] for c in resp.json())

    async def test_no_active_project_shows_only_unfiled(self):
        async with client_app() as (client, app):
            await self._seed(app)
            assert await self._listed(client, None) == ["c-unfiled"]

    async def test_an_active_project_adds_to_unfiled_and_hides_the_other(self):
        async with client_app() as (client, app):
            await self._seed(app)
            # The header names the scope directly, which is what the frontend sends.
            # `c-unfiled` surviving is the point: a pre-projects thread must not vanish.
            assert await self._listed(client, "proj-a") == ["c-a", "c-unfiled"]

    async def test_all_disables_scoping(self):
        async with client_app() as (client, app):
            await self._seed(app)
            assert await self._listed(client, "all") == ["c-a", "c-b", "c-unfiled"]


class TestCreationStampsTheScope:
    """A filter over a column nothing writes is dead weight that looks like a feature.

    Each of these creates through the **real route** with a project active and asserts
    the row came back filed. Without them the scope machinery passes every test it has
    (the seeded rows carry ids by hand) while every surface shows everything.
    """

    @staticmethod
    async def _active(client, tmp_path, name: str) -> str:
        root = tmp_path / name
        root.mkdir()
        project = (
            await client.post("/projects", json={"name": name, "rootPath": str(root)})
        ).json()
        await client.post(f"/projects/{project['id']}/activate")
        return project["id"]

    @staticmethod
    async def _filed(app, model_cls, row_id: str) -> str | None:
        def work(session: Session) -> str | None:
            row = session.get(model_cls, row_id)
            return None if row is None else row.project_id

        return await in_session(app.state.db_engine, work)

    async def test_a_task_is_filed(self, tmp_path):
        async with client_app() as (client, app):
            project_id = await self._active(client, tmp_path, "work")
            created = (
                await client.post(
                    "/tasks",
                    json={
                        "kind": "agent",
                        "title": "T",
                        "prompt": "do it",
                        "output": "notification",
                        "schedule": {"type": "interval", "everySeconds": 3600},
                    },
                )
            ).json()
            assert await self._filed(app, ScheduledTask, created["id"]) == project_id

    async def test_a_corpus_folder_is_filed(self, tmp_path):
        async with client_app() as (client, app):
            project_id = await self._active(client, tmp_path, "work")
            folder = tmp_path / "notes"
            folder.mkdir()
            created = (await client.post("/corpus/folders", json={"path": str(folder)})).json()
            assert await self._filed(app, CorpusSource, created["id"]) == project_id

    async def test_all_projects_files_nothing(self, tmp_path):
        async with client_app() as (client, app):
            await self._active(client, tmp_path, "work")
            created = (
                await client.post(
                    "/tasks",
                    json={
                        "kind": "agent",
                        "title": "T",
                        "prompt": "do it",
                        "output": "notification",
                        "schedule": {"type": "interval", "everySeconds": 3600},
                    },
                    headers={"X-Heph-Project": "all"},
                )
            ).json()
            # Asking to *see* everything is not a statement about where new work goes.
            assert await self._filed(app, ScheduledTask, created["id"]) is None


class TestCorpusScopeIsAUnion:
    """Recall applies the scope as a union, and that difference is deliberate.

    A list narrows to what you are looking at. Recall must not, or a project chat
    loses access to everything the operator ever learned. What it *does* exclude is
    the other direction — another project's sources.
    """

    @staticmethod
    async def _excluded(app, active: str | None) -> frozenset[str]:
        def work(session: Session) -> None:
            session.add(
                CorpusSource(
                    id="s-unfiled",
                    owner_id="operator",
                    kind="folder",
                    project_id=None,
                    path_enc="p",
                )
            )
            session.add(
                CorpusSource(
                    id="s-a", owner_id="operator", kind="folder", project_id="proj-a", path_enc="p"
                )
            )
            session.add(
                CorpusSource(
                    id="s-b", owner_id="operator", kind="folder", project_id="proj-b", path_enc="p"
                )
            )
            session.commit()

        await in_session(app.state.db_engine, work)
        return await app.state.corpus._out_of_scope_source_ids(
            "operator", visible_project_ids(active)
        )

    async def test_unfiled_sources_stay_reachable_from_inside_a_project(self):
        async with client_app() as (_client, app):
            excluded = await self._excluded(app, "proj-a")
            # The union: the operator's general knowledge is still readable, and so is
            # this project's own. Only the other project is cut out.
            assert excluded == frozenset({"s-b"})

    async def test_nothing_is_excluded_when_unscoped(self):
        async with client_app() as (_client, app):
            assert await self._excluded(app, None) == frozenset({"s-a", "s-b"})
