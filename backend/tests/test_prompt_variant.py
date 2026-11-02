"""A trial's rewrites reach what the model is handed, and nothing else."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic_ai import Agent, FunctionToolset
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from agent.variant import VARIANT_ENV, PromptVariant, apply, prompt_variant_capability

RULE = "Three or more steps always gets a list."


def _write(path: Path, *rewrites: tuple[str, str]) -> None:
    body = {"rewrite": [{"find": find, "replace": new} for find, new in rewrites]}
    _ = path.write_text(json.dumps(body))


class _Seen:
    def __init__(self) -> None:
        self.instructions: list[str] = []
        self.system: list[str] = []
        self.tools: list[str] = []
        self.skill_returns: list[str] = []


def _agent(seen: _Seen, variant: PromptVariant[object]) -> Agent[None, str]:
    toolset: FunctionToolset = FunctionToolset()

    @toolset.tool_plain(name="skills_open")
    def skills_open() -> str:
        """Opens a skill. Plan first. Three or more   steps always
        gets a list."""
        return f"Read the brief. {RULE} Then work."

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.instructions.append(info.instructions or "")
        seen.tools += [tool.description or "" for tool in info.function_tools]
        for message in messages:
            if isinstance(message, ModelRequest):
                for part in message.parts:
                    if isinstance(part, SystemPromptPart):
                        seen.system.append(part.content)
                    if isinstance(part, ToolReturnPart):
                        seen.skill_returns.append(str(part.content))
        if not seen.skill_returns:
            return ModelResponse(parts=[ToolCallPart("skills_open", {})])
        return ModelResponse(parts=[TextPart("done")])

    return Agent(
        FunctionModel(model),
        system_prompt=f"You are a test. {RULE}",
        instructions=f"Work carefully.\n{RULE} Report at the end.",
        toolsets=[toolset],
        capabilities=[variant],
    )


async def test_a_removal_reaches_every_part_of_the_brief(tmp_path: Path):
    path = tmp_path / "variant.json"
    _write(path, (RULE, ""))
    seen = _Seen()
    result = await _agent(seen, PromptVariant(path)).run("go")

    assert seen.instructions and all(RULE not in text for text in seen.instructions)
    assert "Report at the end." in seen.instructions[0]
    assert seen.system and all(RULE not in text for text in seen.system)
    assert seen.tools and all("always gets a list" not in text for text in seen.tools)
    assert seen.skill_returns and all(RULE not in text for text in seen.skill_returns)
    persisted = [
        part.content
        for message in result.all_messages()
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, SystemPromptPart)
    ]
    assert any(RULE in text for text in persisted), "the history keeps the original"


async def test_a_replacement_is_applied_where_the_sentence_was(tmp_path: Path):
    path = tmp_path / "variant.json"
    _write(path, (RULE, "Four steps or more get a list."))
    seen = _Seen()
    _ = await _agent(seen, PromptVariant(path)).run("go")

    assert "Work carefully.\nFour steps or more get a list. Report" in seen.instructions[0]


def test_removing_a_list_item_takes_its_whole_line(tmp_path: Path):
    path = tmp_path / "variant.json"
    _write(path, ("- When you pick work back up, check first.", ""))
    rewrites = PromptVariant(path).rewrites()
    text = "Head.\n- One.\n- When you pick work\n  back up, check first.\n- Two."
    assert apply(text, rewrites) == "Head.\n- One.\n- Two."


@pytest.mark.parametrize("content", [None, ""], ids=["missing", "empty"])
async def test_no_file_or_an_empty_one_is_the_base_arm(tmp_path: Path, content: str | None):
    path = tmp_path / "variant.json"
    if content is not None:
        _ = path.write_text(content)
    seen = _Seen()
    _ = await _agent(seen, PromptVariant(path)).run("go")

    assert RULE in seen.instructions[0]


def test_nothing_is_installed_outside_a_trial(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(VARIANT_ENV, raising=False)
    assert prompt_variant_capability() == []
    monkeypatch.setenv(VARIANT_ENV, "/tmp/variant.json")
    assert len(prompt_variant_capability()) == 1
