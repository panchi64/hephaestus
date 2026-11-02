"""Print a prompt as the model receives it: ``uv run python -m devkit.prompts <target>``.

Targets: ``talos`` or ``talos:<level>`` (the main agent's static head and that level's
fragment), ``subagent:<name>`` (a built-in agent's brief), and ``<module>.<CONSTANT>`` for any
string in ``prompts/`` (``utility.TITLE_INSTRUCTIONS``). ``list`` prints the targets;
``corpus`` prints the name of every model-facing source :func:`corpus` collects.
"""

from __future__ import annotations

import argparse
import importlib
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast, get_args

from pydantic_ai.toolsets import AbstractToolset

from prompts.agent import (
    INSTRUCTIONS,
    SPAWNED_INSTRUCTIONS,
    SPAWNED_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
)
from prompts.voice import CLASSES, join
from services.agents.builtin import builtin_roster
from services.agents.definition import WorkspacePolicy
from services.permissions.levels import PERMISSION_LADDER, permission_spec
from services.skills.sources import PLATFORM_ROOT
from services.subagents.briefing import brief_for, workspace_note
from tools import RunDeps
from tools.catalog import tool_catalog

_PROMPT_MODULES = (
    "agent",
    "browse",
    "calendar",
    "compaction",
    "levels",
    "utility",
    "voice",
)


def _module_strings(module: str) -> dict[str, str]:
    namespace: dict[str, object] = vars(importlib.import_module(f"prompts.{module}"))
    return {
        name: value
        for name, value in namespace.items()
        if name.isupper() and isinstance(value, str)
    }


def targets() -> list[str]:
    """Every name :func:`render` accepts."""
    names = ["talos", *(f"talos:{level}" for level in PERMISSION_LADDER)]
    names += [f"subagent:{name}" for name, agent in builtin_roster().items() if agent.delegable]
    names += [f"{module}.{name}" for module in _PROMPT_MODULES for name in _module_strings(module)]
    return names


def render(target: str) -> str:
    """The composed text for one target; raises ``KeyError`` for an unknown one."""
    kind, _, argument = target.partition(":")
    if kind == "talos":
        level = permission_spec(argument).instructions if argument else ""
        return join(SYSTEM_PROMPT, INSTRUCTIONS, level)
    if kind == "subagent":
        agent = builtin_roster()[argument]
        brief = brief_for(agent.instructions, agent.workspace, agent.agent_class)
        return join(SPAWNED_SYSTEM_PROMPT, SPAWNED_INSTRUCTIONS, brief)
    module, _, name = target.partition(".")
    if module not in _PROMPT_MODULES:
        raise KeyError(target)
    return _module_strings(module)[name]


@dataclass(frozen=True, slots=True)
class Source:
    """One piece of model-facing text and the name it is registered under."""

    name: str
    text: str


#: Prompt strings that live beside the code that sends them rather than in ``prompts/``.
_EXTRA_STRINGS: Mapping[str, tuple[str, ...]] = {
    "services.subagents.briefing": ("_CONTRACT",),
    "services.subagents.report": (
        "_REPORT_PREAMBLE",
        "_DIRECTION_PREAMBLE",
        "_MESSAGE_PREAMBLE",
        "FINDINGS_INSTRUCTION",
    ),
    "harness.manifests._subagent_wake": ("OPERATOR_STOPPED", "RESPAWN_HINT", "GRACE_NOTE"),
    "services.commands.expand": ("SKILL_INVOKED", "AGENT_INVOKED"),
    "core.untrusted": ("_INSTRUCTION",),
    "plugins.mail.services.style": ("_LEARN_INSTRUCTIONS", "_DRAFT_INSTRUCTIONS"),
    "plugins.mail.services.triage": ("_INSTRUCTIONS",),
    "plugins.routines.prompts": ("ROUTINE_SCHEDULE_INSTRUCTIONS",),
}


def _defines(module: str, name: str) -> bool:
    """Whether ``prompts.<module>`` assigns ``name`` itself rather than importing it."""
    path = importlib.import_module(f"prompts.{module}").__file__
    assert path is not None
    return re.search(rf"^{name}\s*[:=]", Path(path).read_text(), re.MULTILINE) is not None


def full_tool_categories() -> dict[str, AbstractToolset[RunDeps]]:
    """The category mapping a real app assembles (core plus manifest `toolsets`), unbooted."""
    from harness.discovery import discover_manifests
    from tools.toolsets import core_categories

    categories = core_categories()
    for manifest in discover_manifests():
        for name, factory in manifest.toolsets:
            categories[name] = factory()
    return categories


def corpus() -> list[Source]:
    """Every model-facing text, each under the most specific name it has.

    Sources overlap (``prompts.agent.INSTRUCTIONS`` holds ``prompts.agent._COST``); a reader
    looking for where a sentence lives wants the shortest source that contains it.
    """
    sources = [
        Source(f"prompts.{module}.{name}", text)
        for module in _PROMPT_MODULES
        for name, text in _module_strings(module).items()
        if _defines(module, name)
    ]
    for dotted, names in _EXTRA_STRINGS.items():
        namespace: dict[str, object] = vars(importlib.import_module(dotted))
        for name in names:
            value = namespace[name]
            assert isinstance(value, str), f"{dotted}.{name} is not a string"
            sources.append(Source(f"{dotted}.{name}", value))
    sources += [Source(f"class.{name}", spec.lore) for name, spec in CLASSES.items()]
    sources += [
        Source(f"workspace.{policy}", workspace_note(policy))
        for policy in cast(tuple[WorkspacePolicy, ...], get_args(WorkspacePolicy))
    ]
    sources += [
        Source(f"agent.{name}", agent.instructions) for name, agent in builtin_roster().items()
    ]
    sources += [
        Source(f"skill.{path.relative_to(PLATFORM_ROOT).as_posix()}", path.read_text())
        for path in sorted(PLATFORM_ROOT.rglob("*.md"))
    ]
    sources += [
        Source(f"tool.{info.name}", info.description)
        for info in tool_catalog(full_tool_categories())
    ]
    return sources


class _Args(argparse.Namespace):
    target: str = "list"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m devkit.prompts", description=__doc__)
    _ = parser.add_argument("target", help="what to print, or `list`")
    target = parser.parse_args(argv, namespace=_Args()).target
    if target == "list":
        print("\n".join(targets()))
        return 0
    if target == "corpus":
        print("\n".join(source.name for source in corpus()))
        return 0
    try:
        print(render(target))
    except KeyError:
        print(f"unknown target {target!r}; run with `list` for the valid ones", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
