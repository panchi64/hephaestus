"""The standing brief — what the model is told before it is told anything else.

Every assertion here is about a claim that is only true *sometimes*, which is the whole
reason these parts are dynamic instructions rather than prose in the system prompt. A
fragment that leaks into a thread it does not describe is worse than a missing one: the
model acts on a false premise and has no way to find out.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic_ai import ModelRequest
from pydantic_ai.models.test import TestModel

from agent.factory import build_agent
from core.container import ServiceContainer
from core.timezone import local_zone_key
from prompts.agent import SYSTEM_PROMPT
from prompts.levels import AUTO_LEVEL, MANUAL_LEVEL, PLAN_LEVEL
from prompts.workspace import CHECKOUT_WORKSPACE, WORKTREE_WORKSPACE
from runs import Run, RunStream
from services.permissions import PermissionLevel
from services.workspace_kind import WorkspaceKind
from tools.builtin import builtin_toolset
from tools.deps import RunDeps


async def _brief(*, kind: WorkspaceKind = "sandbox", permission: PermissionLevel = "edit") -> str:
    """The instructions a turn in this thread would actually ship with.

    Driven through a real agent rather than read off the registries, because the thing
    worth pinning is the *registration* — a fragment that exists and is never wired in
    looks identical to one that is, from the registry's side.
    """
    deps = RunDeps(
        run=Run(id="run-1", kind="chat", owner_id="operator", stream=RunStream()),
        owner_id="operator",
        caps=ServiceContainer(),
        conversation_id="conv-1",
        workspace_kind=kind,
        permission=permission,
    )
    agent = build_agent(TestModel(), categories={})
    result = await agent.run("hello", deps=deps)
    requests = [m for m in result.all_messages() if isinstance(m, ModelRequest)]
    return "\n".join(m.instructions or "" for m in requests)


class TestTheLevelPart:
    async def test_each_level_that_contradicts_the_base_posture_says_so(self):
        assert PLAN_LEVEL in await _brief(permission="plan")
        assert MANUAL_LEVEL in await _brief(permission="manual")
        assert AUTO_LEVEL in await _brief(permission="auto")

    async def test_edit_adds_nothing(self):
        """Edit is what the base prompt was written for — act, and the approval gate
        catches what is genuinely dangerous. Restating that here would cost head-of-prompt
        tokens on every turn to agree with the paragraph above it."""
        brief = await _brief(permission="edit")
        for fragment in (PLAN_LEVEL, MANUAL_LEVEL, AUTO_LEVEL):
            assert fragment not in brief

    async def test_no_levels_fragment_reaches_another_level(self):
        """The failure this file exists for: a fragment in a thread it does not describe
        is a false premise the model has no way to check."""
        levels: tuple[tuple[PermissionLevel, str], ...] = (
            ("plan", PLAN_LEVEL),
            ("manual", MANUAL_LEVEL),
            ("auto", AUTO_LEVEL),
        )
        for level, own in levels:
            brief = await _brief(permission=level)
            for fragment in (PLAN_LEVEL, MANUAL_LEVEL, AUTO_LEVEL):
                assert (fragment in brief) is (fragment is own)

    async def test_the_auto_fragment_claims_nothing_about_tools_a_thread_may_not_have(
        self,
    ):
        """Auto is every fresh thread's level, and the tools that take a `reach` are the
        shell's — which only a project-folder thread is offered
        (`services/workspace_kind.WORKSPACE_TOOLS`). So the fragment has to be true in a
        sandbox thread too: it asks the model to answer the question *where a tool
        asks it*, rather than asserting that every command carries one."""
        for kind in ("sandbox", "checkout", "worktree"):
            assert AUTO_LEVEL in await _brief(kind=kind, permission="auto")
        assert "Where a tool asks how far a command needs to reach" in AUTO_LEVEL
        assert "Every shell command" not in AUTO_LEVEL

    async def test_auto_asks_for_the_declaration_and_never_states_the_bar(self):
        """The one thing an Auto thread's model has to *do* differently is declare a
        reach. What clears the review is deliberately absent — from here as much as from
        the reviewer's own prompt: a model told the bar writes for the bar."""
        assert "reach" in AUTO_LEVEL
        for tell in ("low", "high", "too_destructive", "authorization"):
            assert tell not in AUTO_LEVEL

    async def test_a_plan_thread_is_told_its_missing_tools_are_deliberate(self):
        """The catalog is cut at Plan whether or not the model knows why. Without this it
        reads a withheld tool as a broken installation."""
        assert "on purpose" in PLAN_LEVEL


class TestTheWorkspacePart:
    async def test_only_a_project_folder_claims_the_files_are_the_operators(self):
        assert CHECKOUT_WORKSPACE in await _brief(kind="checkout")
        assert WORKTREE_WORKSPACE in await _brief(kind="worktree")
        sandbox = await _brief(kind="sandbox")
        assert CHECKOUT_WORKSPACE not in sandbox
        assert WORKTREE_WORKSPACE not in sandbox

    async def test_each_project_folder_gets_its_own_fragment(self):
        assert WORKTREE_WORKSPACE not in await _brief(kind="checkout")
        assert CHECKOUT_WORKSPACE not in await _brief(kind="worktree")

    async def test_the_path_link_rule_left_the_system_prompt(self):
        """It renders a control only when the files are the operator's own, so in a sandbox
        thread the same syntax produces a link that opens nothing."""
        assert "opens that file in their editor" in CHECKOUT_WORKSPACE
        assert "opens that file in their editor" in WORKTREE_WORKSPACE
        assert "editor" not in SYSTEM_PROMPT


class TestTheSystemPrompt:
    def test_it_no_longer_describes_a_machine_that_may_not_exist(self):
        """The sandbox is `code_execute`'s to describe, said once where the model is
        deciding whether to run something — and a project-folder thread has no sandbox at all,
        so the paragraph was false in a third of threads."""
        lowered = SYSTEM_PROMPT.lower()
        for claim in ("pip", "immutable", "apt", "home directory"):
            assert claim not in lowered


class TestTheDateLine:
    async def test_it_carries_a_zone(self):
        """A date with no zone leaves "tomorrow at nine" resolved against a guess, while
        every tool underneath it already runs on the operator's own clock."""
        brief = await _brief()
        assert f"({local_zone_key()})" in brief


class TestTheClock:
    def test_now_reports_the_operators_own_time_with_its_offset(self):
        stamp = builtin_toolset().tools["now"].function_schema.function()
        parsed = datetime.fromisoformat(stamp)
        assert parsed.utcoffset() is not None
        assert parsed.utcoffset() == datetime.now().astimezone().utcoffset()


class TestTheZoneLookup:
    def test_an_explicit_tz_wins(self, monkeypatch):
        monkeypatch.setenv("TZ", "Europe/Madrid")
        assert local_zone_key() == "Europe/Madrid"

    def test_a_blank_tz_is_the_utc_it_means(self, monkeypatch):
        """POSIX reads a set-but-empty ``TZ`` as UTC. Reading it as "unset" instead sends
        the lookup to ``/etc/localtime`` and names a zone the host's clock is not on."""
        monkeypatch.setenv("TZ", "   ")
        assert local_zone_key() == "UTC"

    def test_a_leading_colon_is_not_part_of_the_key(self, monkeypatch):
        """`glibc` documents the colon and ignores it. Kept, it reaches the calendar as a
        key `ZoneInfo` rejects, so the brief says Madrid while events are filed in UTC."""
        monkeypatch.setenv("TZ", ":Europe/Madrid")
        assert local_zone_key() == "Europe/Madrid"
        assert ZoneInfo(local_zone_key())

    def test_a_tz_holding_a_path_is_read_the_way_the_link_is(self, monkeypatch):
        monkeypatch.setenv("TZ", ":/usr/share/zoneinfo/Europe/Madrid")
        assert local_zone_key() == "Europe/Madrid"

    def test_a_posix_rule_string_answers_with_the_offset_it_puts_the_host_on(self, monkeypatch):
        """`TZ` can hold a rule rather than a name. Handed back verbatim it is a key no
        caller can build a zone from; falling back to `/etc/localtime` would name the very
        zone this `TZ` overrides, so the offset is the only answer still on this clock."""
        monkeypatch.setenv("TZ", "CET-1CEST,M3.5.0,M10.5.0/3")
        assert re.fullmatch(r"UTC[+-]\d{2}:\d{2}", local_zone_key())

    def test_the_zoneinfo_link_names_the_zone(self, monkeypatch, tmp_path):
        monkeypatch.delenv("TZ", raising=False)
        link = tmp_path / "localtime"
        target = tmp_path / "usr" / "share" / "zoneinfo" / "Europe" / "Madrid"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"")
        link.symlink_to(target)
        monkeypatch.setattr("core.timezone._LOCALTIME", link)
        assert local_zone_key() == "Europe/Madrid"

    def test_no_localtime_falls_back_to_the_offset(self, monkeypatch, tmp_path):
        """A container with no tz database still has a real offset, and an offset is worth
        more to a model reasoning about "tomorrow morning" than nothing."""
        monkeypatch.delenv("TZ", raising=False)
        monkeypatch.setattr("core.timezone._LOCALTIME", Path(tmp_path / "absent"))
        assert re.fullmatch(r"UTC[+-]\d{2}:\d{2}", local_zone_key())
