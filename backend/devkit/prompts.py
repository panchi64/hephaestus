"""Print a prompt as the model receives it: ``uv run python -m devkit.prompts <target>``.

Targets: ``talos`` or ``talos:<level>`` (the main agent's static head and that level's
fragment), ``subagent:<name>`` (a built-in agent's brief), and ``<module>.<CONSTANT>`` for any
string in ``prompts/`` (``utility.TITLE_INSTRUCTIONS``). ``list`` prints the targets.
"""

from __future__ import annotations

import argparse
import importlib
import sys

from prompts.agent import (
    INSTRUCTIONS,
    SPAWNED_INSTRUCTIONS,
    SPAWNED_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
)
from prompts.voice import join
from services.agents.builtin import builtin_roster
from services.permissions.levels import PERMISSION_LADDER, permission_spec
from services.subagents.briefing import brief_for

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


class _Args(argparse.Namespace):
    target: str = "list"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m devkit.prompts", description=__doc__)
    _ = parser.add_argument("target", help="what to print, or `list`")
    target = parser.parse_args(argv, namespace=_Args()).target
    if target == "list":
        print("\n".join(targets()))
        return 0
    try:
        print(render(target))
    except KeyError:
        print(f"unknown target {target!r}; run with `list` for the valid ones", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
