"""The roster — what the model may launch, and how a project adds to it.

The roster is the model's whole view of sub-agents: the launch tool's description is
generated from it, and a name the description does not carry is one nothing will ever ask
for. So these hold two kinds of property.

**The built-ins are real.** A spec names tools in `tools.deny` and `required`, and those
names are strings against a catalog that moves. A `required` name that no longer exists
makes the sub-agent permanently unlaunchable; a `deny` one silently stops withholding.
Neither fails anywhere else, so they are pinned here against the catalog a real run
resolves — the same guard `tests/test_tool_sensitivity.py` keeps over its own literal.

**A project's own definitions are the same record.** A file under `.claude/agents/` becomes
an `AgentDefinition` and nothing downstream can tell it from a built-in — which is the claim
that makes the feature worth having, and the one that would quietly stop being true.

The last group is the one worth reading twice: a declaration is a **request**. A project
file can narrow what its sub-agent may do and can never widen it, because the file arrives
with a checkout and nobody approved it.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from core.config import Settings
from services.agents import (
    BUILTIN,
    AgentDefinition,
    builtin_roster,
    delegable,
    merged_roster,
    project_agents,
)
from services.agents.files import AgentFileError, agent_files, parse_agent_file
from tools.catalog import tool_catalog

from ._helpers import full_tool_categories

_AGENT_FILE = """---
name: house-reviewer
description: Reviews a change against this repository's own conventions.
---

You are this project's reviewer. Check the conventions in docs/style.md.
"""


def _write(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class TestTheBuiltIns:
    def test_every_tool_a_spec_names_is_a_tool_that_exists(self):
        catalog = {tool.name for tool in tool_catalog(full_tool_categories())}
        for spec in BUILTIN:
            named = spec.tools.deny | spec.required
            # A `required` name that has been renamed makes the sub-agent permanently
            # unlaunchable; a `deny` one silently stops withholding. Both are strings
            # against a catalog that moves, and neither fails anywhere else.
            assert named <= catalog, f"{spec.name} names tools that are not in the catalog"

    def test_the_ones_that_only_report_cannot_edit(self):
        roster = builtin_roster()
        # Read-only by never being offered a mutating tool, which is the only form of it
        # that survives a model deciding the fix was obvious.
        assert roster["explorer"].permission_ceiling == "plan"
        assert roster["reviewer"].permission_ceiling == "plan"
        # The test runner has to *run* things, so a read-only level would leave it
        # reporting on a suite it never executed — it is narrowed by tool instead.
        assert "files_edit_file" in roster["test_runner"].tools.deny

    def test_a_test_runner_without_a_shell_refuses_rather_than_recites(self):
        # The whole point of `required`: a sub-agent missing the capability its job rests
        # on still answers, from the model's own memory, and reads downstream exactly as
        # though it had run the suite.
        assert "shell_run_command" in builtin_roster()["test_runner"].required

    def test_the_description_the_model_reads_names_every_one_of_them(self):
        from services.agents import describe_roster

        described = describe_roster(builtin_roster())
        for spec in delegable(builtin_roster()).values():
            # Generated rather than written out, so a sub-agent added to the roster is one
            # the model can actually see. A name missing here may as well not exist.
            assert f"`{spec.name}`" in described


class TestTheResearcher:
    """The one built-in that replaced a whole feature.

    Research is one row in the roster, which only works if the row carries everything a
    dedicated research launcher would enforce.
    """

    def test_it_refuses_rather_than_answering_from_memory(self):
        # The direct port of the deleted `_REQUIRED_WEB_TOOLS` check. A researcher with no
        # way to reach the web still answers — from the model's own memory — and reads, in
        # a report, exactly as though it had gone and looked.
        assert builtin_roster()["researcher"].required == frozenset({"web_search", "web_fetch"})

    def test_it_reads_and_therefore_launches_without_asking(self):
        # Reading the web changes nothing, so the operator is not asked. What they rule on
        # is the plan the launching thread submits before it fans out.
        assert builtin_roster()["researcher"].permission_ceiling == "plan"

    def test_it_carries_its_own_request_limit(self):
        # Reading enough sources to see where they disagree is many more round trips than
        # the operator's default turn budget allows for.
        limit = builtin_roster()["researcher"].request_limit
        assert limit is not None
        assert limit > Settings().agent_request_limit

    def test_it_works_nowhere_near_the_launching_threads_files(self):
        # It is reading the open web, which is the case `own` exists for. Sharing would
        # put a long-running reader in the operator's tree for no reason at all.
        assert builtin_roster()["researcher"].workspace == "own"


class TestReadingAProjectsOwnFiles:
    def test_a_definition_becomes_an_ordinary_spec(self):
        spec = parse_agent_file(_AGENT_FILE, name="ignored")

        # The claim the whole feature rests on: past the parser there is no such thing as
        # a "project sub-agent", only a spec.
        assert isinstance(spec, AgentDefinition)
        assert spec.name == "house-reviewer"
        assert "conventions" in spec.description
        assert "docs/style.md" in spec.instructions

    def test_the_filename_names_it_when_the_frontmatter_does_not(self):
        spec = parse_agent_file(
            "---\ndescription: Does a thing.\n---\n\nDo the thing.", name="tidy-up"
        )
        # The standard allows either, and a file is usually named for what is in it.
        assert spec.name == "tidy-up"

    @pytest.mark.parametrize(
        ("text", "why"),
        [
            ("no frontmatter here", "not a definition at all"),
            ("---\nname: x\n---\n\nbody", "nothing for the model to choose it by"),
            ("---\nname: x\ndescription: y\n---\n", "no instructions at all"),
            ("---\nname: Not A Name!\ndescription: y\n---\n\nbody", "unusable name"),
        ],
    )
    def test_a_file_that_is_not_a_usable_definition_is_refused(self, text, why):
        with pytest.raises(AgentFileError):
            parse_agent_file(text, name="fallback")

    def test_a_broken_file_costs_its_own_agent_and_nothing_else(self, tmp_path):
        _write(tmp_path, ".claude/agents/good.md", _AGENT_FILE)
        _write(tmp_path, ".claude/agents/broken.md", "this is not an agent definition")

        specs = project_agents(tmp_path)

        # These are hand-written, they arrive with a checkout, and nobody editing one is
        # watching this process. One bad file must not cost the roster the others.
        assert [spec.name for spec in specs] == ["house-reviewer"]

    def test_a_project_with_no_agent_files_adds_nothing(self, tmp_path):
        assert project_agents(tmp_path) == ()


class TestWhereTheFilesAre:
    def test_it_reads_the_places_the_inventory_tool_reports(self, tmp_path):
        _write(tmp_path, ".claude/agents/one.md", _AGENT_FILE)
        _write(tmp_path, ".codex/agents/two.md", _AGENT_FILE)

        found = {path.name for path in agent_files(tmp_path)}

        # The same scan `repo_inventory_agent_context` answers from, so "where your agent
        # files live" and "which ones were loaded" cannot disagree.
        assert found == {"one.md", "two.md"}

    def test_a_file_sitting_directly_in_dot_agents_counts(self, tmp_path):
        _write(tmp_path, ".agents/flat.md", _AGENT_FILE)

        # A directory named that is an agents directory, whatever shape the scan expects.
        assert [path.name for path in agent_files(tmp_path)] == ["flat.md"]

    def test_the_order_is_stable_between_checkouts(self, tmp_path):
        for name in ("zebra.md", "alpha.md", "middle.md"):
            _write(tmp_path, f".claude/agents/{name}", _AGENT_FILE)

        names = [path.name for path in agent_files(tmp_path)]

        # The order is the order the model reads the descriptions in. A roster that
        # shuffles between runs is a prompt prefix that never caches.
        assert names == sorted(names)


class TestADeclarationIsARequestNotAGrant:
    def test_a_file_declaring_itself_read_only_is_held_to_it(self):
        spec = parse_agent_file(
            "---\nname: looker\ndescription: Looks.\ntools: Read, Grep, Glob\n---\n\nLook.",
            name="looker",
        )
        # The foreign standard's `tools` list cannot be honoured name-for-name — those are
        # another tool's names, and applied literally they would withhold this catalog
        # entirely. What it reliably carries is intent about reach, and the read-only case
        # is the one where that intent is unambiguous.
        assert spec.permission_ceiling == "plan"

    def test_a_file_listing_tools_that_act_is_left_to_the_launcher(self):
        spec = parse_agent_file(
            "---\nname: doer\ndescription: Does.\ntools: Read, Bash, Edit\n---\n\nDo.",
            name="doer",
        )
        # Not raised to anything — the launcher folds in the launching thread's own level,
        # and a file guessing at this installation's levels would be guessing.
        assert spec.permission_ceiling is None

    def test_a_file_may_ask_to_work_out_of_the_way(self):
        spec = parse_agent_file(
            "---\nname: apart\ndescription: Works apart.\nworkspace: isolated\n---\n\nGo.",
            name="apart",
        )
        # Ours rather than the foreign standard's, which has no notion of a workspace
        # because the agent it describes always runs in the one checkout.
        assert spec.workspace == "isolated"

    def test_an_unknown_workspace_costs_the_project_nothing(self):
        spec = parse_agent_file(
            "---\nname: odd\ndescription: Odd.\nworkspace: elsewhere\n---\n\nGo.", name="odd"
        )
        # A typo in an optional field must not cost the project its agent.
        assert spec.workspace == "shared"

    def test_a_project_definition_shadows_a_built_in_of_the_same_name(self):
        mine = parse_agent_file(
            "---\nname: reviewer\ndescription: Ours.\n---\n\nReview our way.", name="reviewer"
        )

        roster = merged_roster(BUILTIN, [mine])

        # A repository that has written down how *its* reviewer should work knows
        # something this installation cannot.
        assert roster["reviewer"].description == "Ours."
        assert len(roster) == len(BUILTIN)


class TestReadingItOncePerRun:
    """`get_tools` runs on every model request, up to the request limit in a single turn."""

    @staticmethod
    def _ctx(run_id):
        return SimpleNamespace(
            deps=SimpleNamespace(
                run=SimpleNamespace(id=run_id), caps=SimpleNamespace(get_optional=lambda _: None)
            )
        )

    @staticmethod
    def _tree(monkeypatch, root):
        async def tree(_ctx):
            return root

        monkeypatch.setattr("tools.project_agents.project_tree", tree)

    async def test_a_runs_files_are_read_once_however_many_requests_it_makes(
        self, tmp_path, monkeypatch
    ):
        from tools.project_agents import run_catalog

        self._tree(monkeypatch, tmp_path)
        _write(tmp_path, ".claude/agents/one.md", _AGENT_FILE)
        first = await run_catalog(self._ctx("run-1"))
        _write(tmp_path, ".claude/agents/one.md", _AGENT_FILE.replace("house-", "other-"))

        # A description that changed mid-turn rewrites the prompt head under the turn and
        # invalidates its prefix cache.
        assert await run_catalog(self._ctx("run-1")) == first

    async def test_the_next_turn_reads_them_again(self, tmp_path, monkeypatch):
        from tools.project_agents import run_catalog

        self._tree(monkeypatch, tmp_path)
        _write(tmp_path, ".claude/agents/one.md", _AGENT_FILE)
        await run_catalog(self._ctx("run-1"))
        _write(tmp_path, ".claude/agents/one.md", _AGENT_FILE.replace("house-", "other-"))

        assert "other-reviewer" in await run_catalog(self._ctx("run-2"))

    async def test_the_memo_does_not_grow_with_every_run_the_process_serves(
        self, tmp_path, monkeypatch
    ):
        from tools.project_agents import _MAX_CATALOGS, _catalogs, run_catalog

        self._tree(monkeypatch, tmp_path)
        for n in range(_MAX_CATALOGS + 10):
            await run_catalog(self._ctx(f"bounded-{n}"))

        assert len(_catalogs) <= _MAX_CATALOGS


class TestWhatTheModelActuallySees:
    """The roster reaching the model is the only part of this that matters.

    A project sub-agent that is loaded, merged and launchable but absent from the launch
    tool's description is one nothing will ever ask for — every test above it passes and
    the feature does nothing.
    """

    async def test_the_launch_tool_is_described_against_the_runs_own_roster(self, monkeypatch):
        mine = parse_agent_file(
            "---\nname: house-style\ndescription: Checks our house style.\n---\n\nCheck it.",
            name="house-style",
        )
        tools = await _launch_tools(monkeypatch, merged_roster(BUILTIN, [mine]))

        # THE assertion of the whole phase: what the model reads when it is deciding
        # carries the project's own agent, not just the ones this installation ships.
        assert "`house-style`" in (tools["launch"].tool_def.description or "")
        assert "Checks our house style." in (tools["launch"].tool_def.description or "")

    async def test_a_run_with_no_project_agents_is_described_exactly_as_it_was_built(
        self, monkeypatch
    ):
        from tools.subagents import launch_description

        tools = await _launch_tools(monkeypatch, builtin_roster())

        # The overwhelmingly common case. The description has to come out byte-identical,
        # or every request of every turn pays a fresh prefix for a rewrite that changed
        # nothing.
        assert tools["launch"].tool_def.description == launch_description(builtin_roster())

    def test_the_catalog_the_operator_reads_is_not_per_run(self):
        from tools.subagents import subagents_toolset

        # The settings surface enumerates `.tools`, which has no run to resolve. A wrapper
        # that stopped forwarding it would silently cost the operator every toggle in this
        # category.
        assert set(subagents_toolset().tools) == {"launch", "send", "list", "read"}

    async def test_the_app_the_operator_runs_has_the_rewrite_in_its_stack(self):
        from tests._helpers import client_app

        async with client_app() as (_client, app):
            category = app.state.tool_categories["subagents"]
            # Built through the manifest rather than by this test: a wrapper that only
            # exists when the test constructs it is a wrapper no run ever passes through.
            assert hasattr(category, "wrapped")


class TestWhichLaunchesAreTheOperatorsToAllow:
    """Only a sub-agent that can change things.

    Two mechanisms could gate this and only one of them can tell them apart. The static
    `requires_approval=True` marking defers a call *before its body runs*, which means the
    library parks on the tool's name with nothing having looked at which sub-agent was
    asked for — so every launch asks, an explorer that can only read included, and the
    conditional check inside the body never decides anything. Raising from inside is what
    makes the distinction real, and these are what notice the marking coming back.
    """

    def test_launching_is_not_gated_by_name_alone(self):
        from tools.subagents import subagents_toolset

        launch = subagents_toolset().tools["launch"]
        assert launch.requires_approval is False

    async def test_the_tool_the_model_is_offered_is_not_pre_deferred(self, monkeypatch):
        tools = await _launch_tools(monkeypatch, builtin_roster())

        # `unapproved` is the kind the static marking produces, and it is what makes the
        # library hold the call back rather than run it.
        assert tools["launch"].tool_def.kind != "unapproved"

    def test_a_sub_agent_that_only_reads_is_launched_without_asking(self):
        from tools.subagents import _can_write

        roster = builtin_roster()
        assert not _can_write(roster["explorer"])
        assert not _can_write(roster["reviewer"])

    def test_a_sub_agent_that_can_change_things_asks_first(self):
        from tools.subagents import _can_write

        roster = builtin_roster()
        # The test runner is here on purpose: it is withheld from *editing*, which is
        # about its job, and still runs commands — so the level, not the withheld list,
        # is what says whether the operator should rule on it.
        assert _can_write(roster["worker"])
        assert _can_write(roster["test_runner"])

    def test_a_project_agent_is_gated_by_what_it_can_do_rather_than_by_its_name(self):
        from tools.subagents import _can_write

        reads = parse_agent_file(
            "---\nname: auditor\ndescription: Reads.\npermission: plan\n---\n\nRead it.",
            name="auditor",
        )
        edits = parse_agent_file(
            "---\nname: fixer\ndescription: Fixes.\npermission: edit\n---\n\nFix it.",
            name="fixer",
        )
        assert not _can_write(reads)
        assert _can_write(edits)


async def _launch_tools(monkeypatch, roster):
    """The tools this category offers a run whose project declares ``roster``.

    The roster is injected rather than written to a real worktree: what is under test here
    is that `get_tools` describes the launch tool from whatever the run's roster is, and a
    worktree would only add a second thing that could fail.
    """
    from pydantic_ai import RunContext
    from pydantic_ai.models.test import TestModel
    from pydantic_ai.usage import RunUsage

    from runs import Run, RunStream
    from tools import RunDeps
    from tools import subagents as module

    monkeypatch.setattr(module, "run_roster", lambda _ctx: _resolved(roster))
    run = Run(id="t", kind="chat", owner_id="operator", stream=RunStream())
    ctx = RunContext(
        deps=RunDeps(run=run, owner_id="operator"), model=TestModel(), usage=RunUsage()
    )
    return await module.subagents_toolset().get_tools(ctx)


async def _resolved(value):
    return value
