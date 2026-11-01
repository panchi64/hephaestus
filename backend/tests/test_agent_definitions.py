"""What an agent definition is: its tool access, and how a project's file becomes one."""

from __future__ import annotations

from dataclasses import replace

import pytest

from services.agents import ToolAccess
from services.agents.definition import AgentDefinition
from services.agents.files import parse_agent_file, render_agent_file


class TestToolAccess:
    def test_none_allows_everything(self):
        assert ToolAccess().permits("files_write_file")

    def test_a_full_name_matches_only_itself(self):
        access = ToolAccess(allow=frozenset({"files_read_file"}))
        assert access.permits("files_read_file")
        assert not access.permits("files_write_file")

    def test_a_category_covers_every_tool_under_it(self):
        access = ToolAccess(allow=frozenset({"files"}))
        assert access.permits("files_write_file")
        assert not access.permits("shell_run_command")

    def test_a_category_is_not_a_substring_match(self):
        assert not ToolAccess(allow=frozenset({"file"})).permits("files_read_file")

    def test_deny_beats_allow(self):
        access = ToolAccess(allow=frozenset({"files"}), deny=frozenset({"files_write_file"}))
        assert access.permits("files_read_file")
        assert not access.permits("files_write_file")

    def test_deny_alone_withholds_only_what_it_names(self):
        access = ToolAccess(deny=frozenset({"shell"}))
        assert not access.permits("shell_run_command")
        assert access.permits("files_read_file")

    def test_an_empty_allow_offers_nothing(self):
        assert not ToolAccess(allow=frozenset()).permits("files_read_file")


def _file(extra: str = "") -> str:
    return f"---\nname: scout\ndescription: Looks around.\n{extra}---\n\nLook around.\n"


class TestParsingTools:
    def test_it_is_a_project_agent(self):
        assert parse_agent_file(_file(), name="scout").source == "project"

    def test_our_tool_names_become_the_allow_list(self):
        agent = parse_agent_file(_file("tools: files, web_search\n"), name="scout")
        assert agent.tools.allow == frozenset({"files", "web_search"})
        assert agent.tools.deny == frozenset()

    def test_a_yaml_list_is_read_too(self):
        agent = parse_agent_file(_file("tools:\n  - files_read_file\n"), name="scout")
        assert agent.tools.allow == frozenset({"files_read_file"})

    def test_disallowed_tools_become_the_deny_list(self):
        for key in ("disallowed-tools", "disallowedTools"):
            agent = parse_agent_file(_file(f"{key}: shell\n"), name="scout")
            assert agent.tools.deny == frozenset({"shell"})
            assert agent.tools.allow is None

    def test_a_list_of_only_claude_code_names_is_not_applied(self):
        agent = parse_agent_file(_file("tools: Read, Grep, Glob\n"), name="scout")
        assert agent.tools.allow is None

    def test_a_read_only_list_still_infers_the_read_only_ceiling(self):
        agent = parse_agent_file(_file("tools: Read, Grep, Glob\n"), name="scout")
        assert agent.permission_ceiling == "plan"

    def test_foreign_disallowed_names_are_not_applied(self):
        agent = parse_agent_file(_file("disallowedTools: Write, Edit\n"), name="scout")
        assert agent.tools.deny == frozenset()

    @pytest.mark.parametrize("raw", ["", "tools:\n"])
    def test_no_tools_means_unrestricted(self, raw: str):
        assert parse_agent_file(_file(raw), name="scout").tools == ToolAccess()


class TestMaxRequests:
    def test_it_is_parsed_from_frontmatter(self):
        assert parse_agent_file(_file("max-requests: 12\n"), name="scout").max_requests == 12

    @pytest.mark.parametrize("raw", ["0", "-1", "-3", "0.5", "2.9", "true", "lots", "abc"])
    def test_an_invalid_value_is_ignored_without_raising(self, raw: str):
        assert parse_agent_file(_file(f"max-requests: {raw}\n"), name="scout").max_requests is None

    def test_the_definition_refuses_a_non_positive_value(self):
        with pytest.raises(ValueError, match="positive"):
            _ = AgentDefinition(name="scout", description="x", max_requests=0)

    def test_it_round_trips_through_an_exported_file(self):
        agent = parse_agent_file(_file("max-requests: 7\n"), name="scout")
        assert parse_agent_file(render_agent_file(agent), name="scout").max_requests == 7
        assert "max-requests" not in render_agent_file(replace(agent, max_requests=None))
