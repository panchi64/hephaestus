"""Agents a project declares for itself, in its own files.

The convention is Claude Code's: a markdown file with YAML frontmatter under
``.claude/agents/`` (or the other asset roots the repo-inventory tool reports, or flat in
``.agents/``). A project definition is an :class:`AgentDefinition` built from a file, and
past this module nothing can tell it from a built-in.

**A bad file is skipped, never fatal.** These arrive with a checkout and nobody editing
one is watching this process, so a file that cannot be read costs its own agent and is
logged.

**A declaration is a request, never a grant.** What a file asks for is reported honestly
here and folded against the launching thread's own level at launch.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from pathlib import Path
from typing import cast

import yaml
from pydantic_ai_harness.repo_context import RepoContext

import core.text
from core.frontmatter import FrontmatterError, split_frontmatter
from prompts.voice import CLASSES
from services.agents.definition import (
    DEFAULT_CLASS,
    UNASSIGNABLE_CLASSES,
    AgentDefinition,
    ToolAccess,
    WorkspacePolicy,
    class_key,
    workspace_policy,
)
from services.permissions import PERMISSION_LEVELS, STRICTEST_PERMISSION

logger = logging.getLogger(__name__)

#: The roots scanned for ``agents/*.md``, shared with the repo-inventory tool so both see
#: one filesystem layout.
ASSET_ROOTS: tuple[str, ...] = tuple(RepoContext.asset_roots)

#: Read flat as well as through ``<root>/agents/``: a ``reviewer.md`` placed directly here
#: is an agent.
FLAT_ROOT = ".agents"

#: Looser than the skills' rule: underscores are allowed, because the built-ins use them and
#: shadowing a built-in means naming the file to match.
_NAME = re.compile(r"^[a-z0-9]([a-z0-9_-]*[a-z0-9])?$")

#: Descriptions are joined into the launch tool's description, which is sent on every
#: request of every turn.
DESCRIPTION_MAX_CHARS = 400

#: The brief is charged per request of the sub-agent's own turns, so the cap is larger; it
#: guards against a file that is not a brief (a design document under ``agents/``).
BRIEF_MAX_CHARS = 8_000

_WHITESPACE_RUN = re.compile(r"\s+")

#: Tool names from the foreign standard that observe and nothing more. See
#: :func:`_declared_ceiling` for what is and is not done with them.
_READ_ONLY_FOREIGN_TOOLS = frozenset(
    {
        "read",
        "glob",
        "grep",
        "ls",
        "notebookread",
        "todoread",
        "webfetch",
        "websearch",
    }
)

#: Every tool name in the foreign standard. A ``tools`` list made only of these is read for
#: its reach (see :func:`_declared_ceiling`); one naming anything else is ours.
_FOREIGN_TOOLS = _READ_ONLY_FOREIGN_TOOLS | frozenset(
    {
        "bash",
        "edit",
        "multiedit",
        "notebookedit",
        "task",
        "todowrite",
        "write",
        "killshell",
        "bashoutput",
    }
)


class AgentNameError(ValueError):
    """``normalize_agent_name`` was handed something that is not a usable name.

    Each caller translates it for its own audience: a bad ``name:`` in a project file is
    skipped and logged, while a bad launch handle goes back to the model as a retry.
    """


class AgentFileError(ValueError):
    """The file is not a usable agent definition."""


def project_agents(root: Path) -> tuple[AgentDefinition, ...]:
    """Every agent this project declares, by name, in discovery order. Never raises."""
    specs: dict[str, AgentDefinition] = {}
    for path in agent_files(root):
        try:
            spec = parse_agent_file(path.read_text(encoding="utf-8"), name=path.stem)
        except (AgentFileError, OSError, UnicodeDecodeError) as exc:
            logger.info("agents: skipped agent file %s (%s)", path, exc)
            continue
        specs[spec.name] = spec
    return tuple(specs.values())


def agent_files(root: Path) -> list[Path]:
    """The agent definition files under a project root, deduplicated and ordered.

    Sorted so the roster order is stable across checkouts; a shuffling roster never caches
    as a prompt prefix.
    """
    found: set[Path] = set()
    for directory in [*(root / name / "agents" for name in ASSET_ROOTS), root / FLAT_ROOT]:
        if directory.is_dir():
            found.update(path.resolve() for path in directory.glob("*.md") if path.is_file())
    return sorted(found)


def parse_agent_file(text: str, *, name: str, clip: bool = True) -> AgentDefinition:
    """One agent definition file as a spec.

    ``name`` is the filename's stem, used when the frontmatter names no agent. ``clip``
    trims an over-long description or brief to what a project file may cost; a file the
    operator wrote keeps its text and is validated by the store instead.
    """
    try:
        loaded, body = split_frontmatter(text)
    except FrontmatterError as exc:
        raise AgentFileError(str(exc)) from None

    fields: dict[str, object] = loaded
    spec_name = _name(fields.get("name"), fallback=name)
    description = _text(fields.get("description"), field="description")
    if not description:
        raise AgentFileError(f"agent {spec_name!r} has no description")
    brief = body.strip()
    if not brief:
        raise AgentFileError(f"agent {spec_name!r} has no instructions below its frontmatter")

    try:
        return AgentDefinition(
            name=spec_name,
            description=core.text.clip(description, DESCRIPTION_MAX_CHARS) if clip else description,
            agent_class=_declared_class(fields, name=spec_name),
            instructions=core.text.clip(brief, BRIEF_MAX_CHARS) if clip else brief,
            tools=_declared_tools(fields),
            permission_ceiling=_declared_ceiling(fields, name=spec_name),
            workspace=_declared_workspace(fields, name=spec_name),
            wall_clock_timeout_s=_positive(fields.get("wall-clock-timeout"), float),
            can_delegate=fields.get("can-delegate") is True,
            max_depth=_positive(fields.get("max-depth"), int),
            max_requests=_positive(fields.get("max-requests"), int),
            source="project",
        )
    except ValueError as exc:
        raise AgentFileError(f"agent {spec_name!r}: {exc}") from None


def render_agent_file(agent: AgentDefinition) -> str:
    """``agent`` as an agent file that :func:`parse_agent_file` reads back. The model is
    left out: it names an endpoint of this installation."""
    front: dict[str, object] = {
        "name": agent.name,
        "description": agent.description,
        "class": agent.agent_class,
    }
    if agent.tools.allow is not None:
        front["tools"] = ", ".join(sorted(agent.tools.allow))
    if agent.tools.deny:
        front["disallowed-tools"] = ", ".join(sorted(agent.tools.deny))
    if agent.permission_ceiling is not None:
        front["permission"] = agent.permission_ceiling
    if agent.workspace != "shared":
        front["workspace"] = agent.workspace
    if agent.wall_clock_timeout_s is not None:
        front["wall-clock-timeout"] = agent.wall_clock_timeout_s
    if agent.can_delegate:
        front["can-delegate"] = True
    if agent.max_depth is not None:
        front["max-depth"] = agent.max_depth
    if agent.max_requests is not None:
        front["max-requests"] = agent.max_requests
    dumped = yaml.safe_dump(front, sort_keys=False, allow_unicode=True, default_flow_style=False)
    return f"---\n{dumped}---\n\n{agent.instructions}\n"


def normalize_agent_name(raw: str) -> str:
    """One name rule, for every name a sub-agent is addressed by.

    Covers both the ``name:`` in a project's agent file (a definition) and the handle the
    launching model picks for a sub-agent (an instance). Spaces fold to hyphens rather than
    being rejected: ``Helper Function Finder`` can only have meant one thing.

    Raises :class:`AgentNameError`.
    """
    value = raw.strip().lower().replace(" ", "-")
    if not _NAME.match(value):
        message = (
            f"{value!r} is not a usable agent name — lowercase letters, digits, "
            "hyphens and underscores only"
        )
        raise AgentNameError(message)
    return value


def _name(raw: object, *, fallback: str) -> str:
    try:
        return normalize_agent_name(_text(raw) or fallback)
    except AgentNameError as exc:
        # Callers recognise a skipped file by AgentFileError alone.
        raise AgentFileError(str(exc)) from None


def _text(raw: object, *, field: str | None = None) -> str:
    """A frontmatter scalar as one line of text.

    Newlines collapse rather than reject: a YAML block scalar is normal, and the roster
    renders one line regardless.
    """
    if raw is None:
        return ""
    if not isinstance(raw, str):
        if field is None:
            return ""
        raise AgentFileError(f"{field} must be text")
    return _WHITESPACE_RUN.sub(" ", raw).strip()


def _declared_class(fields: dict[str, object], *, name: str) -> str:
    """The ``class:`` key as a class key. Unlike an unknown workspace, an unknown class
    fails the file: it decides the agent's role, voice and callsigns, and no fallback is
    a safe guess at the author's intent."""
    declared = _text(fields.get("class"), field="class")
    if not declared:
        return DEFAULT_CLASS
    key = class_key(declared)
    if key is None or key in UNASSIGNABLE_CLASSES:
        known = ", ".join(k for k in CLASSES if k not in UNASSIGNABLE_CLASSES)
        kind = "unknown" if key is None else "reserved"
        raise AgentFileError(f"agent {name!r} names {kind} class {declared!r}; use one of {known}")
    return key


def _declared_workspace(fields: dict[str, object], *, name: str) -> WorkspacePolicy:
    """Which files this agent works in, if the file says.

    Our own field; the foreign standard has no workspace. An unrecognised value falls back
    to the default rather than failing the file.
    """
    declared = _text(fields.get("workspace")).lower()
    if not declared:
        return "shared"
    policy = workspace_policy(declared)
    if policy is None:
        logger.info("agents: agent %r declared unknown workspace %r; using shared", name, declared)
        return "shared"
    return policy


def _declared_ceiling(fields: dict[str, object], *, name: str) -> str | None:
    """How far this agent may reach, read from whichever of the two fields says something.

    ``permission`` is ours and names one of this installation's levels directly.

    ``tools`` is the foreign standard's and is read for one bit only: whether everything it
    lists observes. Its names (``Read``, ``Grep``, ``Bash``) are not this catalog's, so an
    allow-list applied literally would withhold every tool. Anything not read-only is left
    to the launcher's level, which the launching thread's own already caps.
    """
    declared = _text(fields.get("permission")).lower()
    if declared in PERMISSION_LEVELS:
        return declared
    if declared:
        # The fallback level is capped by the launching thread's own, so a wrong guess can
        # only narrow the sub-agent.
        logger.info(
            "agents: agent %r declared unknown permission %r; using the default level",
            name,
            declared,
        )
    listed = _tool_names(fields.get("tools"))
    if listed and listed <= _READ_ONLY_FOREIGN_TOOLS:
        return STRICTEST_PERMISSION
    return None


def _declared_tools(fields: dict[str, object]) -> ToolAccess:
    """The tools this agent is offered, when the file names them in this catalog's terms.

    A list made only of the foreign standard's names (``Read``, ``Bash``) cannot be
    applied literally — it would withhold every tool here — so it is read for reach only,
    by :func:`_declared_ceiling`. ``disallowed-tools`` is read the same way.
    """
    allow = _tool_names(fields.get("tools"))
    deny = _tool_names(fields.get("disallowed-tools") or fields.get("disallowedTools"))
    return ToolAccess(
        allow=allow if allow and not allow <= _FOREIGN_TOOLS else None,
        deny=deny if deny and not deny <= _FOREIGN_TOOLS else frozenset(),
    )


def _positive[T: (int, float)](raw: object, kind: type[T]) -> T | None:
    """A positive number from frontmatter, or None for anything else."""
    if isinstance(raw, bool) or not isinstance(raw, int | float) or raw <= 0:
        return None
    if kind is int and not isinstance(raw, int):
        return None
    return kind(raw)


def _tool_names(raw: object) -> frozenset[str]:
    """The ``tools`` field's names, lowercased. Accepts the standard's comma-separated
    string and a YAML list, since both are written in the wild."""
    parts: Iterable[str]
    if isinstance(raw, str):
        parts = re.split(r"[,\s]+", raw)
    elif isinstance(raw, list):
        parts = [str(part) for part in cast("list[object]", raw)]
    else:
        return frozenset()
    return frozenset(part.strip().lower() for part in parts if part.strip())
