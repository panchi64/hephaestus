"""Tool sensitivity classes, and the read-only catalog they make possible.

`services/tool_sensitivity.py` names every tool as a literal string, because `tools/` sits
above `services/` and importing the catalog there would invert the dependency order. The
literal is checked against the catalog a real run resolves, in both directions. The direction that
actually rots is the second one. A tool added without a class would keep running under a
level that was supposed to withhold it, and nothing else in the system would notice.

The rest is the narrowing itself: that Plan is the only level that takes tools out of the
catalog, that it takes out everything that would change something *except* the plan writes
a Plan turn exists to make, and that a real run resolves the same answer through the
composed toolset stack rather than through a re-derivation of the rule.
"""

from __future__ import annotations

from services.permissions import PERMISSION_LEVELS, PLANNING_TOOLS
from services.tool_policy import (
    effective_disabled_tools,
    permission_disabled_tools,
    set_tool_enabled,
)
from services.tool_sensitivity import (
    SENSITIVITY_CLASSES,
    UNCLASSIFIED,
    Sensitivity,
    sensitivity_of,
    tools_above,
)
from tools.catalog import tool_catalog

from ._helpers import full_tool_categories
from .test_tool_policy import agent_visible, make_store, stub_offline

OWNER = "operator"


def _catalog_names() -> set[str]:
    return {t.name for t in tool_catalog(full_tool_categories())}


def _classified() -> set[str]:
    return {name for names in SENSITIVITY_CLASSES.values() for name in names}


class TestEveryToolIsClassified:
    def test_every_classified_name_is_a_real_tool(self):
        assert _classified() <= _catalog_names()

    def test_every_real_tool_is_classified(self):
        # The half that rots. A new tool with no class would be silently treated as an
        # external effect — which is safe, but it would also be *wrong* for the reads,
        # withheld from a Plan turn that should have had it.
        assert _catalog_names() <= _classified()

    def test_the_chassis_tools_are_classified_outside_the_catalog(self):
        # `run_code` is offered on every agent by the chassis, not by a category, so it is
        # classified without being pinned to a catalog row — and it is a read: the script
        # touches nothing, and each tool it calls is gated on its own.
        from services.tool_sensitivity import CHASSIS_TOOLS, classified

        assert CHASSIS_TOOLS == {"run_code": Sensitivity.READ}
        assert classified("run_code") and sensitivity_of("run_code") is Sensitivity.READ
        assert not set(CHASSIS_TOOLS) & _catalog_names()

    def test_no_tool_is_in_two_classes(self):
        total = sum(len(names) for names in SENSITIVITY_CLASSES.values())
        assert total == len(_classified())

    def test_every_class_is_used(self):
        # A class nothing carries is a distinction the system does not actually make.
        assert all(names for names in SENSITIVITY_CLASSES.values())
        assert set(SENSITIVITY_CLASSES) == set(Sensitivity)


class TestTheClassesAgreeWithTheExistingMarkings:
    def test_an_approval_marked_tool_is_never_a_read(self):
        # The classes were seeded from the markings, and the two must not drift apart: a
        # tool the operator is asked about before it runs is by definition not one that
        # merely observes.
        categories = full_tool_categories()
        for info in tool_catalog(categories):
            toolset = categories[info.category]
            name = info.name.removeprefix(f"{info.category}_")
            tool = getattr(toolset, "tools", {}).get(name)
            if getattr(tool, "requires_approval", False):
                assert sensitivity_of(info.name).above(Sensitivity.READ), info.name

    def test_the_tools_a_workspace_withholds_are_all_effects(self):
        # `shell`, `repo` and `code` are scoped by workspace kind because of what they reach; the
        # one read among them (`shell_check_command`) is scoped by the category it lives
        # in, not by its own class.
        assert sensitivity_of("shell_run_command") is Sensitivity.HOST_EXEC
        assert sensitivity_of("code_execute") is Sensitivity.HOST_EXEC

    def test_stopping_a_process_is_not_starting_one(self):
        # `shell_stop_command` executes nothing new: it kills a process an already-approved
        # `start_command` created and removes that process's temp files. Classed as host
        # execution it would have parked a run for permission to *end* something the
        # operator had already agreed to start — and at a level whose ceiling stops below
        # host execution, the model could start a background server and never stop it.
        assert sensitivity_of("shell_stop_command") is Sensitivity.WORKSPACE_WRITE
        assert sensitivity_of("shell_check_command") is Sensitivity.READ


class TestResolvingAName:
    def test_a_known_name_resolves_to_its_class(self):
        assert sensitivity_of("web_search") is Sensitivity.READ
        assert sensitivity_of("files_write_file") is Sensitivity.WORKSPACE_WRITE
        assert sensitivity_of("mail_send") is Sensitivity.EXTERNAL_EFFECT
        assert sensitivity_of("vault_get_entry") is Sensitivity.SECRET

    def test_an_operators_external_tool_is_treated_as_an_external_effect(self):
        # `external_{slug}_{tool}` names come from the operator's own MCP servers and
        # connectors, so no literal here can enumerate them. Unknown means unbounded.
        assert sensitivity_of("external_acme_deploy") is UNCLASSIFIED
        assert UNCLASSIFIED is Sensitivity.EXTERNAL_EFFECT

    def test_reading_a_page_is_still_reading(self):
        # Network reach is not the axis. A fetch leaves nothing different behind it; a
        # click on the same page, in the same browser session, can.
        assert sensitivity_of("web_fetch") is Sensitivity.READ
        assert sensitivity_of("browse_snapshot") is Sensitivity.READ
        assert sensitivity_of("browse_click") is Sensitivity.EXTERNAL_EFFECT


class TestEscalation:
    def test_a_read_escalates_past_nothing(self):
        assert not Sensitivity.READ.above(Sensitivity.READ)
        assert Sensitivity.WORKSPACE_WRITE.above(Sensitivity.READ)
        assert Sensitivity.SECRET.above(Sensitivity.WORKSPACE_WRITE)

    def test_the_three_effect_classes_are_not_ordered_among_themselves(self):
        # They are kinds of reach, not degrees of one. A policy that wants to treat a
        # shell command differently from a sent email has to name them.
        for one in (Sensitivity.HOST_EXEC, Sensitivity.EXTERNAL_EFFECT, Sensitivity.SECRET):
            for other in (
                Sensitivity.HOST_EXEC,
                Sensitivity.EXTERNAL_EFFECT,
                Sensitivity.SECRET,
            ):
                assert not one.above(other)

    def test_tools_above_read_is_everything_that_changes_something(self):
        above = tools_above(Sensitivity.READ)
        assert _classified() - above == SENSITIVITY_CLASSES[Sensitivity.READ]
        assert "files_read_file" not in above
        assert "files_write_file" in above


class TestPlanNarrowsTheCatalog:
    def test_only_plan_withholds_anything(self):
        for level in PERMISSION_LEVELS - {"plan"}:
            assert permission_disabled_tools(level) == frozenset()
        assert permission_disabled_tools("plan")

    def test_an_unknown_level_is_read_only(self):
        # Fail closed: a corrupt stored value leaves the model able to look and to plan.
        assert permission_disabled_tools("nonsense") == permission_disabled_tools("plan")
        assert permission_disabled_tools("") == permission_disabled_tools("plan")

    def test_plan_keeps_every_read(self):
        withheld = permission_disabled_tools("plan")
        assert not withheld & SENSITIVITY_CLASSES[Sensitivity.READ]

    def test_plan_keeps_the_two_surfaces_it_ends_with(self):
        # The one exemption. A read-only turn that could not record what it decided would
        # have no way to end — and one that could not reach `plan_submit` would have no
        # way *out*, since that call is the only thing that ends this level.
        withheld = permission_disabled_tools("plan")
        assert not withheld & PLANNING_TOOLS
        # Named rather than left to the set alone: a rename that emptied `PLANNING_TOOLS`
        # would satisfy the line above while withholding everything it protects.
        assert "plan_submit" not in withheld
        assert "tasks_write" not in withheld

    def test_the_exemption_is_not_a_no_op(self):
        # The set names the whole task list, and its writes are what the rank rule would
        # otherwise have taken away — the read in it is exempt anyway and is listed so the
        # exemption reads as one surface rather than half of one.
        assert PLANNING_TOOLS <= _catalog_names()
        writes = {n for n in PLANNING_TOOLS if sensitivity_of(n) is not Sensitivity.READ}
        assert writes
        for name in writes:
            assert sensitivity_of(name) is Sensitivity.WORKSPACE_WRITE

    def test_plan_withholds_every_other_effect(self):
        withheld = permission_disabled_tools("plan")
        assert {
            "code_execute",
            "files_write_file",
            "mail_send",
            "memory_forget",
            "memory_remember",
            "shell_run_command",
            "vault_get_entry",
        } <= withheld


class TestWhatAPlanTurnActuallySees:
    async def test_the_agent_is_offered_reads_and_both_planning_surfaces(self):
        # Through the composed toolset stack a real run resolves, not a re-derivation of
        # the rule: a withheld tool is one the model can neither see nor invoke.
        visible = await agent_visible(permission_disabled_tools("plan"))
        assert {"files_read_file", "web_search", "corpus_retrieve"} <= visible
        # Its own scratchpad, and the way out of the level. `plan_submit` above all: a
        # Plan turn that could not reach it would have no ending at all.
        assert {"tasks_write", "tasks_read"} <= visible
        assert {"plan_enter", "plan_read", "plan_submit"} <= visible
        # And the question. The Plan-level brief tells the model to ask rather than assume
        # where a request is ambiguous (`prompts/levels.py`) — this is the level where
        # asking is cheapest and a guess is most expensive — so the tool it is told to
        # reach for has to survive the narrowing that defines the level.
        assert "builtin_ask_user" in visible
        assert not {"files_write_file", "code_execute", "mail_send"} & visible

    async def test_a_plan_turn_carries_fewer_schemas_than_a_full_one(self):
        # The cost half of the argument: the tools a Plan turn cannot use are schemas it
        # does not pay for either. Every tool it loses is one that would change something.
        full = await agent_visible(frozenset())
        planning = await agent_visible(permission_disabled_tools("plan"))
        assert planning < full
        assert full - planning == permission_disabled_tools("plan") & full


class TestTheUnion:
    async def test_the_sources_compose(self):
        store = make_store()
        _ = await set_tool_enabled(store, OWNER, "builtin_now", False)
        offline = stub_offline(frozenset({"web_search"}))
        disabled = await effective_disabled_tools(store, offline, OWNER)
        assert "builtin_now" in disabled  # the operator's
        assert "web_search" in disabled  # offline's
        # ...and nothing takes back what nothing withheld.
        assert "files_read_file" not in disabled

    async def test_the_level_is_not_one_of_them(self):
        """It withholds too, but it is applied live at the gate rather than unioned here.

        The distinction is the whole reason plan mode can be entered and left inside a
        turn: a union records no provenance, so a level folded in could be added and never
        correctly removed — and taking the Plan set back out would lift the operator's own
        hold on any name they had also switched off. Pinned as an absence because that is
        the property a future "just add it to the union" would quietly break."""
        store = make_store()
        _ = await set_tool_enabled(store, OWNER, "files_write_file", False)
        disabled = await effective_disabled_tools(store, stub_offline(frozenset()), OWNER)
        assert "files_write_file" in disabled  # the operator's, and *only* theirs
        assert permission_disabled_tools("plan") - disabled

    async def test_a_caller_with_no_level_is_unaffected(self):
        store = make_store()
        offline = stub_offline(frozenset())
        assert await effective_disabled_tools(store, offline, OWNER) == frozenset()
