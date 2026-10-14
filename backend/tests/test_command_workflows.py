"""Operator-authored workflows, and the commands a project declares in its own files.

The two sources the catalog did not have. They are different enough to be worth separating
in the reader's head and identical past the point of construction: one is a row the operator
saved, one is a markdown file that arrived with a checkout, and from ``CommandSpec`` onwards
nothing can tell them apart.

What is defended here is mostly what happens when something is *wrong* — a name that could
not be typed, a file with no template, a description written as a list — because both
sources are hand-authored and the failure modes are the whole difference between a picker
that degrades and one that disappears.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI

from core.exceptions import CommandValidationError
from services.commands import CommandRegistry
from services.commands.definitions import (
    CommandFileError,
    parse_command_file,
    project_specs,
)
from services.commands.registry import _merge
from services.commands.store import CommandStore

from ._helpers import app_state, client_app
from ._json import arr, json_body, json_list, obj
from ._json import s as string


def _write(root: Path, relpath: str, text: str) -> None:
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_text(text, encoding="utf-8")


def _store(app: FastAPI) -> CommandStore:
    return app_state(app, "command_store", CommandStore)


def _registry(app: FastAPI) -> CommandRegistry:
    return app_state(app, "commands", CommandRegistry)


class TestWorkflowStore:
    async def test_a_saved_workflow_becomes_a_command(self):
        async with client_app() as (client, app):
            resp = await client.post(
                "/commands/workflows",
                json={"name": "standup", "body": "Summarise what changed since yesterday."},
            )
            assert resp.status_code == 201, resp.text
            specs = await _store(app).specs("operator")
        assert [s.name for s in specs] == ["standup"]
        assert specs[0].source == "workflow"
        assert specs[0].body == "Summarise what changed since yesterday."
        # No title written, so the name stands in — a picker row is never blank.
        assert specs[0].title == "standup"

    async def test_a_name_is_normalised_rather_than_refused(self):
        async with client_app() as (client, _app):
            resp = await client.post(
                "/commands/workflows",
                # Both mistakes are the obvious ones to make: typing the slash that appears
                # everywhere else, and thinking about the ritual rather than the identifier.
                json={"name": "/Stand Up", "body": "b"},
            )
        assert resp.status_code == 201, resp.text
        assert json_body(resp)["name"] == "stand-up"

    async def test_a_name_that_could_not_be_typed_names_its_field(self):
        async with client_app() as (client, _app):
            resp = await client.post("/commands/workflows", json={"name": "what?!", "body": "b"})
        assert resp.status_code == 422
        # The editor marks a box rather than printing "invalid" over the whole form.
        assert obj(json_body(resp)["detail"])["field"] == "name"

    async def test_a_workflow_with_no_template_is_refused(self):
        async with client_app() as (client, _app):
            resp = await client.post("/commands/workflows", json={"name": "empty", "body": "   "})
        assert resp.status_code == 422
        assert obj(json_body(resp)["detail"])["field"] == "body"

    async def test_two_workflows_cannot_share_a_name(self):
        async with client_app() as (client, _app):
            _ = await client.post("/commands/workflows", json={"name": "dup", "body": "b"})
            resp = await client.post("/commands/workflows", json={"name": "dup", "body": "b"})
        assert resp.status_code == 422
        assert obj(json_body(resp)["detail"])["field"] == "name"

    async def test_a_disabled_workflow_leaves_the_menu_but_not_the_editor(self):
        async with client_app() as (client, app):
            created = await client.post("/commands/workflows", json={"name": "draft", "body": "b"})
            _ = await client.patch(
                f"/commands/workflows/{string(json_body(created)['id'])}", json={"enabled": False}
            )
            specs = await _store(app).specs("operator")
            listed = await client.get("/commands/workflows")
        assert specs == []
        # Still editable — this is a switch, not a delete, and the pane is where its
        # author left it.
        assert [obj(row)["name"] for row in json_list(listed)] == ["draft"]

    async def test_an_edit_leaves_the_fields_it_was_not_given(self):
        async with client_app() as (client, _app):
            created = await client.post(
                "/commands/workflows",
                json={"name": "notes", "body": "original", "description": "d"},
            )
            resp = await client.patch(
                f"/commands/workflows/{created.json()['id']}", json={"name": "release-notes"}
            )
        assert resp.status_code == 200, resp.text
        assert json_body(resp)["name"] == "release-notes"
        assert json_body(resp)["body"] == "original"
        assert json_body(resp)["description"] == "d"

    async def test_the_catalog_offers_it_and_the_turn_resolves_it(self):
        async with client_app() as (client, _app):
            _ = await client.post(
                "/commands/workflows",
                json={
                    "name": "standup",
                    "body": "Summarise what changed since yesterday.",
                    "description": "The morning summary",
                },
            )
            listed = await client.get("/commands")
        rows = {string(obj(row)["name"]): obj(row) for row in arr(json_body(listed)["commands"])}
        assert rows["standup"]["group"] == "workflow"
        assert rows["standup"]["description"] == "The morning summary"
        # The body is the expansion's input, never the catalog's payload — a menu that
        # shipped every template would cost a page of prose to render a list of names.
        assert "body" not in rows["standup"]

    async def test_the_workflows_group_is_named_by_the_backend(self):
        async with client_app() as (client, _app):
            _ = await client.post("/commands/workflows", json={"name": "s", "body": "b"})
            listed = await client.get("/commands")
        groups = {string(obj(g)["id"]): obj(g)["label"] for g in arr(json_body(listed)["groups"])}
        assert groups["workflow"] == "Workflows"

    async def test_a_deleted_workflow_is_gone_from_both(self):
        async with client_app() as (client, app):
            created = await client.post("/commands/workflows", json={"name": "temp", "body": "b"})
            resp = await client.delete(f"/commands/workflows/{created.json()['id']}")
            assert resp.status_code == 204
            specs = await _store(app).specs("operator")
            listed = await client.get("/commands/workflows")
        assert specs == []
        assert json_list(listed) == []


class TestProjectFiles:
    """``.claude/commands/*.md`` — the same job ``services/subagents/definitions`` does one
    directory over, and held to the same standard: a bad file costs its own command."""

    def test_a_file_with_frontmatter_is_read_whole(self, tmp_path: Path):
        _write(
            tmp_path,
            ".claude/commands/release-notes.md",
            "---\ndescription: Draft the notes\nargument-hint: the version\n---\n"
            + "Write release notes from the log.\n",
        )
        specs = project_specs(tmp_path)
        assert [s.name for s in specs] == ["release-notes"]
        assert specs[0].source == "project"
        assert specs[0].description == "Draft the notes"
        assert specs[0].argument_hint == "the version"
        assert specs[0].body == "Write release notes from the log."

    def test_a_bare_prompt_file_needs_no_frontmatter(self, tmp_path: Path):
        # The smallest useful command in the wild, and the one an agent file may not be:
        # nobody but the operator chooses a command, so there is nothing a description is
        # needed *for*.
        _write(tmp_path, ".claude/commands/standup.md", "# What changed\n\nSummarise it.\n")
        specs = project_specs(tmp_path)
        assert specs[0].name == "standup"
        # The first line, with the heading marks off — the picker renders no markdown.
        assert specs[0].description == "What changed"

    def test_a_file_with_no_template_is_skipped(self, tmp_path: Path):
        _write(tmp_path, ".claude/commands/hollow.md", "---\ndescription: d\n---\n\n")
        _write(tmp_path, ".claude/commands/real.md", "Do the thing.")
        # Skipped, not fatal: nobody is watching this checkout while the operator types.
        assert [s.name for s in project_specs(tmp_path)] == ["real"]

    def test_a_name_that_could_not_be_typed_is_skipped(self, tmp_path: Path):
        _write(tmp_path, ".claude/commands/fine.md", "---\nname: what?!\n---\nbody")
        _write(tmp_path, ".claude/commands/other.md", "body")
        assert [s.name for s in project_specs(tmp_path)] == ["other"]

    def test_a_description_of_the_wrong_type_falls_back(self, tmp_path: Path):
        _write(
            tmp_path,
            ".claude/commands/listy.md",
            "---\ndescription:\n  - one\n  - two\n---\nDo the thing.",
        )
        # Losing a whole command over a mistyped optional field would be the worse trade.
        assert project_specs(tmp_path)[0].description == "Do the thing."

    def test_every_asset_root_is_read_including_the_flat_one(self, tmp_path: Path):
        _write(tmp_path, ".claude/commands/a.md", "a")
        _write(tmp_path, ".agents/commands/b.md", "b")
        _write(tmp_path, ".commands/c.md", "c")
        assert {s.name for s in project_specs(tmp_path)} == {"a", "b", "c"}

    def test_the_order_is_the_same_in_two_checkouts(self, tmp_path: Path):
        for name in ("zebra", "alpha", "middle"):
            _write(tmp_path, f".claude/commands/{name}.md", "body")
        # Sorted by path, not by whatever order the filesystem happened to hand back.
        assert [s.name for s in project_specs(tmp_path)] == ["alpha", "middle", "zebra"]

    def test_a_project_command_is_offered_only_where_the_checkout_is(self, tmp_path: Path):
        _write(tmp_path, ".claude/commands/deploy.md", "body")
        # Project folders only. A sandbox thread has no repository to have declared this,
        # and the body it would carry came from files that thread cannot see.
        assert project_specs(tmp_path)[0].workspaces == ("checkout", "worktree")

    def test_a_long_template_is_clipped_rather_than_dropped(self, tmp_path: Path):
        _write(tmp_path, ".claude/commands/essay.md", "x" * 20_000)
        spec = project_specs(tmp_path)[0]
        assert len(spec.body or "") < 20_000
        assert (spec.body or "").endswith("…")

    def test_two_files_claiming_one_name_keep_the_first(self, tmp_path: Path):
        _write(tmp_path, ".claude/commands/a.md", "---\nname: dup\n---\nfrom a")
        _write(tmp_path, ".claude/commands/b.md", "---\nname: dup\n---\nfrom b")
        specs = project_specs(tmp_path)
        assert len(specs) == 1
        assert specs[0].body == "from a"

    def test_a_directory_with_no_commands_is_simply_empty(self, tmp_path: Path):
        assert project_specs(tmp_path) == ()

    def test_a_bad_frontmatter_block_reads_as_none(self):
        # An unterminated fence is not a broken command — it is a file that opens with
        # three dashes, which the operator is allowed to do in a prompt.
        spec = parse_command_file("---\nnot closed\nstill going", name="odd")
        assert spec.body is not None and "not closed" in spec.body

    def test_an_empty_file_is_an_error_the_caller_catches(self):
        with pytest.raises(CommandFileError):
            _ = parse_command_file("", name="blank")


class TestPrecedenceAcrossTheNewSources:
    def test_a_project_file_outranks_the_operators_own_workflow(self, tmp_path: Path):
        _write(tmp_path, ".claude/commands/deploy.md", "the project's way")
        project = project_specs(tmp_path)[0]
        from services.commands.spec import CommandSpec

        workflow = CommandSpec(
            name="deploy",
            source="workflow",
            kind="prompt",
            title="deploy",
            description="d",
            body="my way",
        )
        entries = _merge([workflow, project])
        winner = next(e.spec for e in entries if e.shadowed_by is None)
        # The repository knows something this installation does not, which is the same
        # rule ``services/subagents/roster`` already states one level down.
        assert winner.source == "project"
        # And the operator's own is still in the catalog, addressable by its full name.
        shadowed = next(e for e in entries if e.shadowed_by is not None)
        assert shadowed.shadowed_by == "project:deploy"


class TestTheCatalogReadsTheCheckout:
    """Both project-declared sources reach the catalog through one ``root``, resolved by
    the route. Without it they contribute nothing — which is the right answer for a thread
    that has no checkout, and the wrong one to reach by accident."""

    @pytest.mark.parametrize("kind", ["checkout", "worktree"])
    async def test_a_project_declares_both_a_command_and_a_sub_agent(
        self, tmp_path: Path, kind: str
    ):
        _write(tmp_path, ".claude/commands/deploy.md", "ship it")
        _write(
            tmp_path,
            ".claude/agents/scout.md",
            "---\ndescription: reads ahead\n---\nLook around and report.",
        )
        async with client_app() as (_client, app):
            entries = await _registry(app).catalog(
                "operator", workspace_kind=kind, has_conversation=True, root=tmp_path
            )
        by_name = {e.spec.name: e.spec for e in entries}
        assert by_name["deploy"].source == "project"
        # A project's *agent* is still an agent — filing it under "Project" would split one
        # roster across two headings in the menu.
        assert by_name["scout"].source == "agent"

    @pytest.mark.parametrize("kind", ["checkout", "worktree"])
    async def test_a_project_agent_replaces_the_builtin_of_that_name(
        self, tmp_path: Path, kind: str
    ):
        _write(
            tmp_path,
            ".claude/agents/explorer.md",
            "---\ndescription: our own explorer\n---\nSearch this repository's way.",
        )
        async with client_app() as (_client, app):
            entries = await _registry(app).catalog(
                "operator", workspace_kind=kind, has_conversation=True, root=tmp_path
            )
        rows = [e.spec for e in entries if e.spec.name == "explorer"]
        # One row, not two: this is the roster's own shadowing, which is also what resolves
        # the name at launch — so the picker and the launcher cannot disagree.
        assert len(rows) == 1
        assert rows[0].description == "our own explorer"

    async def test_without_a_root_the_project_sources_are_simply_absent(self):
        async with client_app() as (_client, app):
            entries = await _registry(app).catalog(
                "operator", workspace_kind="worktree", has_conversation=True
            )
        assert all(e.spec.source != "project" for e in entries)

    async def test_a_project_sub_agent_is_withheld_outside_a_project_folder(self, tmp_path: Path):
        _write(
            tmp_path,
            ".claude/agents/scout.md",
            "---\ndescription: reads ahead\n---\nLook around and report.",
        )
        async with client_app() as (_client, app):
            entries = await _registry(app).catalog(
                "operator", workspace_kind="sandbox", has_conversation=True, root=tmp_path
            )
        # A thread files under a project in *every* kind of workspace, so a resolvable root
        # is not the question — `tools/project_agents.run_roster` reads a project's
        # declarations only in a project folder. Offering `/scout` here would put a row in
        # the menu whose launch resolves to nothing, and the operator would meet that as a
        # failed tool call mid-turn rather than as an option that was never there.
        assert all(e.spec.name != "scout" for e in entries)
        # The built-ins are still offered — it is the project layer that is withheld.
        assert any(e.spec.name == "explorer" for e in entries)

    @pytest.mark.parametrize("kind", ["checkout", "worktree"])
    async def test_two_roster_names_differing_only_by_separator_ship_once(
        self, tmp_path: Path, kind: str
    ):
        _write(
            tmp_path,
            ".claude/agents/test-runner.md",
            "---\ndescription: ours\n---\nRun them our way.",
        )
        async with client_app() as (_client, app):
            entries = await _registry(app).catalog(
                "operator", workspace_kind=kind, has_conversation=True, root=tmp_path
            )
        rows = [e for e in entries if e.spec.name == "test-runner"]
        # The built-in is `test_runner` and the underscore folds to a hyphen, so both
        # roster entries want one typed handle. Shipping both would give the loser a
        # `shadowed_by` pointing at its own qualified name, and `resolve` would pick
        # whichever came first without saying so.
        assert len(rows) == 1
        assert rows[0].shadowed_by is None

    async def test_a_project_command_is_withheld_outside_a_project_folder(self, tmp_path: Path):
        _write(tmp_path, ".claude/commands/deploy.md", "ship it")
        async with client_app() as (_client, app):
            entries = await _registry(app).catalog(
                "operator", workspace_kind="sandbox", has_conversation=True, root=tmp_path
            )
        assert all(e.spec.name != "deploy" for e in entries)

    @pytest.mark.parametrize("kind", ["checkout", "worktree"])
    async def test_a_project_command_resolves_by_name_on_the_way_out(
        self, tmp_path: Path, kind: str
    ):
        _write(tmp_path, ".claude/commands/deploy.md", "ship it carefully")
        async with client_app() as (_client, app):
            spec = await _registry(app).resolve(
                "operator", "deploy", workspace_kind=kind, has_conversation=True, root=tmp_path
            )
        # The send has to reach the same tree the menu listed from, or a picked command
        # would expand to nothing the moment the operator pressed enter.
        assert spec is not None
        assert spec.body == "ship it carefully"


class TestAuthoringRules:
    """One rule, two authors — the form and the file must agree about what a name is."""

    @pytest.mark.parametrize("raw", ["what?!", "-leading", "trailing-", "", "   "])
    def test_the_form_refuses_what_the_file_scan_skips(self, raw: str, tmp_path: Path):
        from services.commands.authoring import validate_name

        with pytest.raises(CommandValidationError):
            _ = validate_name(raw)
        _write(tmp_path, ".claude/commands/f.md", f"---\nname: {raw!r}\n---\nbody")
        assert all(s.name != raw for s in project_specs(tmp_path))
