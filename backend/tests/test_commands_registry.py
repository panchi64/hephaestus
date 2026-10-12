"""The command catalog: which sources answer, what narrows them, and who wins a name."""

from __future__ import annotations

from services.commands import CommandRegistry, expand
from services.commands.registry import LAUNCH_TOOL, _merge
from services.commands.spec import CommandSpec
from services.permissions import PERMISSION_LADDER

from ._helpers import client_app


def _spec(name: str, source: str, **overrides) -> CommandSpec:
    return CommandSpec(
        name=name,
        source=source,  # type: ignore[arg-type]
        kind="prompt",
        title=name,
        description="d",
        **overrides,
    )


class TestPrecedence:
    """Locality ranks a shared name — and never drops the loser."""

    def test_the_more_local_source_wins_the_bare_name(self):
        entries = _merge([_spec("reviewer", "agent"), _spec("reviewer", "skill")])
        winners = [e.spec for e in entries if e.shadowed_by is None]
        assert [w.source for w in winners] == ["skill"]

    def test_the_shadowed_row_still_ships_naming_its_winner(self):
        entries = _merge([_spec("reviewer", "agent"), _spec("reviewer", "skill")])
        # Both are real things the operator may have meant; hiding one would be a
        # deletion wearing a precedence rule.
        assert len(entries) == 2
        shadowed = next(e for e in entries if e.shadowed_by is not None)
        assert shadowed.spec.source == "agent"
        assert shadowed.shadowed_by == "skill:reviewer"

    def test_a_project_command_outranks_an_operator_workflow(self):
        entries = _merge([_spec("deploy", "workflow"), _spec("deploy", "project")])
        winner = next(e.spec for e in entries if e.shadowed_by is None)
        assert winner.source == "project"

    def test_distinct_names_shadow_nothing(self):
        entries = _merge([_spec("a", "agent"), _spec("b", "skill")])
        assert all(e.shadowed_by is None for e in entries)


class TestCatalog:
    async def test_the_builtin_sources_answer(self):
        async with client_app() as (_client, app):
            registry: CommandRegistry = app.state.commands
            entries = await registry.catalog(
                "operator", workspace_kind="sandbox", has_conversation=True
            )
            sources = {e.spec.source for e in entries}
            assert "action" in sources
            assert "agent" in sources

    async def test_a_composer_with_no_thread_is_offered_no_thread_actions(self):
        async with client_app() as (_client, app):
            registry: CommandRegistry = app.state.commands
            names = {
                e.spec.name
                for e in await registry.catalog(
                    "operator", workspace_kind="sandbox", has_conversation=False
                )
            }
            assert "compact" not in names
            assert "fork" not in names
            # Starting a thread is exactly what you do when you have none.
            assert "new" in names

    async def test_withholding_the_launch_tool_withholds_every_agent_command(self):
        async with client_app() as (_client, app):
            registry: CommandRegistry = app.state.commands
            entries = await registry.catalog(
                "operator",
                workspace_kind="sandbox",
                has_conversation=True,
                disabled_tools={LAUNCH_TOOL},
            )
            assert not [e for e in entries if e.spec.source == "agent"]

    async def test_only_published_skills_are_invocable(self):
        async with client_app() as (client, app):
            await client.post(
                "/skills",
                json={"name": "release-notes", "description": "d", "body": "b"},
            )
            registry: CommandRegistry = app.state.commands

            async def skill_names() -> set[str]:
                entries = await registry.catalog(
                    "operator", workspace_kind="sandbox", has_conversation=True
                )
                return {e.spec.name for e in entries if e.spec.source == "skill"}

            # A draft is no more reachable by hand than it is by the model — publish stays
            # the one trust boundary rather than growing a second, quieter one.
            assert await skill_names() == set()
            created = await client.post(
                "/skills", json={"name": "other", "description": "d", "body": "b"}
            )
            await client.post(f"/skills/{created.json()['id']}/publish")
            assert "other" in await skill_names()


class TestResolve:
    async def test_a_bare_name_resolves_the_winner(self):
        async with client_app() as (_client, app):
            registry: CommandRegistry = app.state.commands
            spec = await registry.resolve(
                "operator", "reviewer", workspace_kind="sandbox", has_conversation=True
            )
            assert spec is not None
            assert spec.source == "agent"
            assert spec.target == "reviewer"

    async def test_a_qualified_name_resolves_exactly(self):
        async with client_app() as (_client, app):
            registry: CommandRegistry = app.state.commands
            spec = await registry.resolve(
                "operator", "agent:test-runner", workspace_kind="sandbox", has_conversation=True
            )
            assert spec is not None
            # The command name hyphenates; the roster name it launches does not.
            assert spec.target == "test_runner"

    async def test_an_unknown_name_resolves_to_nothing(self):
        async with client_app() as (_client, app):
            registry: CommandRegistry = app.state.commands
            assert (
                await registry.resolve(
                    "operator", "not-a-command", workspace_kind="sandbox", has_conversation=True
                )
                is None
            )

    async def test_a_withheld_command_cannot_be_reached_by_typing_its_name(self):
        async with client_app() as (_client, app):
            registry: CommandRegistry = app.state.commands
            # The name is the one part of the exchange the operator authors by hand, so
            # resolution runs against the same narrowed catalog the picker was served.
            assert (
                await registry.resolve(
                    "operator",
                    "compact",
                    workspace_kind="sandbox",
                    has_conversation=False,
                )
                is None
            )


class TestRoute:
    async def test_groups_are_served_with_the_rows(self):
        async with client_app() as (client, _app):
            body = (await client.get("/commands")).json()
            assert body["groups"], "the picker's headings come from the backend"
            offered = {c["group"] for c in body["commands"]}
            # No heading without rows under it — an empty group is a promise the operator
            # has to click to discover is empty.
            assert {g["id"] for g in body["groups"]} == offered

    async def test_rows_are_camel_case_and_carry_a_qualified_name(self):
        async with client_app() as (client, _app):
            body = (await client.get("/commands")).json()
            row = next(c for c in body["commands"] if c["name"] == "reviewer")
            assert row["qualifiedName"] == "agent:reviewer"
            assert row["kind"] == "prompt"
            assert row["actionId"] is None
            assert row["shadowedBy"] is None

    async def test_an_action_row_names_an_id_and_never_a_route(self):
        async with client_app() as (client, _app):
            body = (await client.get("/commands?conversation_id=c1")).json()
            row = next(c for c in body["commands"] if c["name"] == "compact")
            assert row["kind"] == "action"
            assert row["actionId"] == "compact"
            assert set(row) & {"method", "path", "params"} == set()

    async def test_the_level_action_offers_the_levels_in_a_stable_order(self):
        async with client_app() as (client, _app):
            body = (await client.get("/commands")).json()
            row = next(c for c in body["commands"] if c["name"] == "level")
            # A set has no order to offer, and string hashing is randomised per process —
            # so a picker built from one would list the levels differently each boot.
            assert row["actionArgument"]["choices"] == list(PERMISSION_LADDER)
            assert row["actionArgument"]["required"] is True


class TestExpansion:
    def test_a_skill_command_directs_the_model_to_open_it(self):
        spec = _spec("release-notes", "skill", target="release-notes")
        block = expand(spec, "")
        assert "skills_open" in block
        assert "'release-notes'" in block

    def test_an_agent_command_carries_the_brief_verbatim(self):
        spec = _spec("reviewer", "agent", target="reviewer")
        block = expand(spec, "check the auth path")
        assert "subagents_launch" in block
        # A sub-agent starts from an empty history, so a brief left implicit in the
        # prompt above describes nothing it can act on.
        assert "check the auth path" in block

    def test_an_agent_command_without_a_brief_says_so(self):
        block = expand(_spec("reviewer", "agent", target="reviewer"), "   ")
        assert "no further instructions" in block

    def test_an_action_expands_to_nothing(self):
        spec = CommandSpec(
            name="compact",
            source="action",
            kind="action",
            title="t",
            description="d",
            action="compact",
        )
        assert expand(spec, "") == ""

    def test_a_template_body_is_delimited_but_not_fenced_as_untrusted(self):
        block = expand(_spec("review", "project", body="Check the diff."), "the auth route")
        assert "--- begin review ---" in block
        assert "Check the diff." in block
        # The untrusted fence tells the model not to follow what is inside it, and a
        # template the operator invoked is precisely what they want followed.
        assert "UNTRUSTED" not in block
        assert "the auth route" in block
