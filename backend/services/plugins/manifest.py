"""A plugin's ``plugin.yaml``, and the rules a plugin's files must meet.

```yaml
name: weather
description: Forecasts from Open-Meteo.
requirements: [httpx]           # pip requirement lines, installed into the plugin's venv
egress: [api.open-meteo.com]    # the only hosts its code can reach
secrets:                        # values the operator sets; the code sees a placeholder
  OPEN_METEO_KEY: api.open-meteo.com
tools:
  - name: forecast
    description: The forecast for a place.
    entry: weather:forecast     # module:function, under the plugin's own files
    sensitivity: read           # read | workspace_write | external_effect (default)
    parameters:                 # JSON Schema for the arguments
      type: object
      properties: {city: {type: string}}
      required: [city]
```

Beside it, ``agents/<name>.md`` files contribute agents and ``skills/<name>/SKILL.md``
bundles contribute skills, available while the plugin is published.

A secret's value never reaches the plugin: its environment variable holds a placeholder
that the egress gateway swaps for the value, in requests to the secret's own host only.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

import yaml
from jsonschema import Draft202012Validator, SchemaError

from core.exceptions import (
    InvalidInputError,
    PluginValidationError,
    RoutineValidationError,
    SkillValidationError,
)
from core.frontmatter import FrontmatterError, split_frontmatter
from services.agents.definition import AgentDefinition
from services.agents.files import AgentFileError, AgentNameError, parse_agent_file
from services.egress import normalise_domain
from services.routines.schedule import parse_schedule
from services.skills.bundle import SKILL_FILE, ParsedSkill, parse_skill_md
from services.tool_sensitivity import Sensitivity

MANIFEST_FILE = "plugin.yaml"
BUNDLE_MAX_BYTES = 1024 * 1024
BUNDLE_MAX_FILES = 200
DESCRIPTION_MAX_CHARS = 1024
TOOLS_MAX = 32
#: A tool is offered as ``plugins_<plugin>_<tool>``, and providers cap a tool name at 64.
_TOOL_NAME_MAX = 64 - len("plugins__")

_NAME = re.compile(r"[a-z][a-z0-9_]{0,39}")
_SECRET = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
_ENTRY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*:[A-Za-z_][A-Za-z0-9_]*")
_REQUIREMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\-\[\],<>=!~ ;'\"]*")
_SENSITIVITIES = {
    "read": Sensitivity.READ,
    "workspace_write": Sensitivity.WORKSPACE_WRITE,
    "external_effect": Sensitivity.EXTERNAL_EFFECT,
}


@dataclass(frozen=True)
class PluginTool:
    name: str
    description: str
    entry: str
    parameters: dict[str, Any]
    sensitivity: Sensitivity

    @property
    def module(self) -> str:
        return self.entry.partition(":")[0]

    @property
    def function(self) -> str:
        return self.entry.partition(":")[2]


@dataclass(frozen=True)
class PluginSkill:
    skill: ParsedSkill
    #: The skill's bundled files, by path within the skill.
    files: tuple[tuple[str, bytes], ...]


@dataclass(frozen=True)
class RoutineTemplate:
    """A routine a plugin offers for the operator to set up: ``routines/<name>.md``, with
    ``title``, ``schedule`` (the compact form `routines_write` takes) and an optional
    ``agent`` in its frontmatter, and the prompt as its body."""

    name: str
    title: str
    schedule: str
    agent: str | None
    prompt: str


@dataclass(frozen=True)
class PluginManifest:
    name: str
    description: str
    requirements: tuple[str, ...]
    egress: tuple[str, ...]
    #: Secret name → the host its value may be sent to.
    secrets: Mapping[str, str]
    tools: tuple[PluginTool, ...]
    #: Agents from ``agents/<name>.md``, in the format a project's agent files use.
    agents: tuple[AgentDefinition, ...] = ()
    #: Skills from ``skills/<name>/SKILL.md`` and the files beside it.
    skills: tuple[PluginSkill, ...] = ()
    #: Routine templates from ``routines/<name>.md``.
    routines: tuple[RoutineTemplate, ...] = ()

    def tool(self, name: str) -> PluginTool | None:
        return next((tool for tool in self.tools if tool.name == name), None)


def validate_name(raw: str) -> str:
    name = raw.strip().lower()
    if not _NAME.fullmatch(name):
        raise PluginValidationError(
            "name", f"{raw!r} is not a plugin name: lowercase letters, digits and underscores"
        )
    return name


def validate_files(files: Mapping[str, bytes]) -> PluginManifest:
    """The bundle's manifest, once every path and the total size are within bounds."""
    if len(files) > BUNDLE_MAX_FILES:
        raise PluginValidationError("files", f"a plugin holds at most {BUNDLE_MAX_FILES} files")
    if sum(len(blob) for blob in files.values()) > BUNDLE_MAX_BYTES:
        raise PluginValidationError(
            "files", f"a plugin is at most {BUNDLE_MAX_BYTES // 1024} KB of files"
        )
    for path in files:
        validate_path(path)
    raw = files.get(MANIFEST_FILE)
    if raw is None:
        raise PluginValidationError("files", f"a plugin needs a {MANIFEST_FILE}")
    try:
        text = raw.decode()
    except UnicodeDecodeError:
        raise PluginValidationError(MANIFEST_FILE, "is not UTF-8 text") from None
    manifest = parse_manifest(text)
    manifest = replace(
        manifest, agents=_agents(files), skills=_skills(files), routines=_routines(files)
    )
    if not (manifest.tools or manifest.agents or manifest.skills or manifest.routines):
        raise PluginValidationError(
            "tools", "a plugin contributes a tool, an agent, a skill or a routine"
        )
    for tool in manifest.tools:
        module = tool.module.replace(".", "/")
        if f"{module}.py" not in files and f"{module}/__init__.py" not in files:
            raise PluginValidationError(
                "tools",
                f"tool {tool.name!r} names module {tool.module!r}, which is not in the files",
            )
    return manifest


def _agents(files: Mapping[str, bytes]) -> tuple[AgentDefinition, ...]:
    agents: list[AgentDefinition] = []
    for path, blob in sorted(files.items()):
        folder, _, filename = path.rpartition("/")
        if folder != "agents" or not filename.endswith(".md"):
            continue
        try:
            agent = parse_agent_file(blob.decode(), name=filename.removesuffix(".md"))
        except (AgentFileError, AgentNameError, UnicodeDecodeError) as exc:
            raise PluginValidationError("agents", f"{path}: {exc}") from None
        agents.append(replace(agent, source="plugin", published=True, id=None))
    if len({agent.name for agent in agents}) != len(agents):
        raise PluginValidationError("agents", "two agents share a name")
    return tuple(agents)


def _skills(files: Mapping[str, bytes]) -> tuple[PluginSkill, ...]:
    skills: list[PluginSkill] = []
    for path, blob in sorted(files.items()):
        parts = path.split("/")
        if len(parts) != 3 or parts[0] != "skills" or parts[2] != SKILL_FILE:
            continue
        prefix = f"skills/{parts[1]}/"
        try:
            parsed = parse_skill_md(blob.decode())
        except (SkillValidationError, UnicodeDecodeError) as exc:
            raise PluginValidationError("skills", f"{path}: {exc}") from None
        bundled = tuple(
            (other.removeprefix(prefix), content)
            for other, content in sorted(files.items())
            if other.startswith(prefix) and other != path
        )
        skills.append(PluginSkill(parsed, bundled))
    if len({entry.skill.name for entry in skills}) != len(skills):
        raise PluginValidationError("skills", "two skills share a name")
    return tuple(skills)


def _routines(files: Mapping[str, bytes]) -> tuple[RoutineTemplate, ...]:
    templates: list[RoutineTemplate] = []
    for path, blob in sorted(files.items()):
        folder, _, filename = path.rpartition("/")
        if folder != "routines" or not filename.endswith(".md"):
            continue
        try:
            fields, body = split_frontmatter(blob.decode())
            template = RoutineTemplate(
                name=validate_name(filename.removesuffix(".md")),
                title=_required(fields, "title"),
                schedule=_required(fields, "schedule"),
                agent=str(fields["agent"]) if fields.get("agent") else None,
                prompt=body.strip(),
            )
            if not template.prompt:
                raise PluginValidationError("routines", "the body is the routine's prompt")
            parse_schedule(template.schedule, "UTC")
        except (FrontmatterError, RoutineValidationError, UnicodeDecodeError) as exc:
            raise PluginValidationError("routines", f"{path}: {exc}") from None
        except PluginValidationError as exc:
            raise PluginValidationError("routines", f"{path}: {exc}") from None
        templates.append(template)
    return tuple(templates)


def _required(fields: Mapping[str, Any], key: str) -> str:
    value = str(fields.get(key) or "").strip()
    if not value:
        raise PluginValidationError("routines", f"needs a {key}")
    return value


def validate_path(path: str) -> str:
    parts = path.split("/")
    if (
        not path
        or path.startswith("/")
        or "\\" in path
        or "\0" in path
        or any(part in ("", ".", "..") for part in parts)
        or len(path) > 200
    ):
        raise PluginValidationError("files", f"{path!r} is not a usable file path")
    return path


def parse_manifest(text: str) -> PluginManifest:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise PluginValidationError(MANIFEST_FILE, f"is not valid YAML: {exc}") from None
    if not isinstance(data, dict):
        raise PluginValidationError(MANIFEST_FILE, "must be a mapping")
    unknown = set(data) - {"name", "description", "requirements", "egress", "secrets", "tools"}
    if unknown:
        raise PluginValidationError(MANIFEST_FILE, f"unknown key {sorted(unknown)[0]!r}")
    name = validate_name(_text(data, "name"))
    description = _description(data, "description")
    requirements = tuple(_requirement(line) for line in _list(data, "requirements"))
    egress = tuple(sorted({_domain("egress", raw) for raw in _list(data, "egress")}))
    secrets = _secrets(data.get("secrets") or {})
    raw_tools = _list(data, "tools", items=dict)
    if len(raw_tools) > TOOLS_MAX:
        raise PluginValidationError("tools", f"a plugin contributes at most {TOOLS_MAX} tools")
    tools = tuple(_tool(raw) for raw in raw_tools)
    names = [tool.name for tool in tools]
    if len(set(names)) != len(names):
        raise PluginValidationError("tools", "two tools share a name")
    for tool in tools:
        if len(name) + len(tool.name) > _TOOL_NAME_MAX:
            raise PluginValidationError(
                "tools", f"{name}_{tool.name} is too long a tool name: shorten one of the two"
            )
    return PluginManifest(name, description, requirements, egress, secrets, tools)


def _tool(raw: dict[str, Any]) -> PluginTool:
    unknown = set(raw) - {"name", "description", "entry", "parameters", "sensitivity"}
    if unknown:
        raise PluginValidationError("tools", f"unknown tool key {sorted(unknown)[0]!r}")
    name = _text(raw, "name", field="tools")
    if not _NAME.fullmatch(name):
        raise PluginValidationError(
            "tools", f"{name!r} is not a tool name: lowercase letters, digits and underscores"
        )
    entry = _text(raw, "entry", field="tools")
    if not _ENTRY.fullmatch(entry):
        raise PluginValidationError("tools", f"{entry!r} is not a `module:function` entry")
    parameters = raw.get("parameters") or {"type": "object", "properties": {}}
    if not isinstance(parameters, dict) or parameters.get("type") != "object":
        raise PluginValidationError("tools", f"{name}: parameters must be an object schema")
    try:
        Draft202012Validator.check_schema(parameters)
    except SchemaError as exc:
        raise PluginValidationError("tools", f"{name}: {exc.message}") from None
    sensitivity = _SENSITIVITIES.get(str(raw.get("sensitivity", "external_effect")))
    if sensitivity is None:
        raise PluginValidationError(
            "tools", f"{name}: sensitivity is one of {', '.join(_SENSITIVITIES)}"
        )
    return PluginTool(
        name, _description(raw, "description", field="tools"), entry, parameters, sensitivity
    )


def _secrets(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise PluginValidationError("secrets", "secrets map each name to the host it is for")
    secrets: dict[str, str] = {}
    for name, host in raw.items():
        if not isinstance(name, str) or not _SECRET.fullmatch(name):
            raise PluginValidationError(
                "secrets", f"{name!r} is not a secret name: upper case, digits and underscores"
            )
        if not isinstance(host, str):
            raise PluginValidationError("secrets", f"{name}: name the host the value is for")
        secrets[name] = secret_domain(host)
    return secrets


def secret_domain(raw: str) -> str:
    """The one host a secret's value may be sent to."""
    domain = _domain("secrets", raw)
    if domain.startswith("*."):
        raise PluginValidationError(
            "secrets", f"{raw!r}: a secret goes to one host, not a wildcard"
        )
    return domain


def _domain(field: str, raw: object) -> str:
    try:
        return normalise_domain(str(raw))
    except InvalidInputError as exc:
        raise PluginValidationError(field, str(exc)) from None


def _requirement(raw: object) -> str:
    line = str(raw).strip()
    if not _REQUIREMENT.fullmatch(line) or line.startswith("-"):
        raise PluginValidationError("requirements", f"{raw!r} is not a requirement line")
    return line


def _text(data: Mapping[str, Any], key: str, *, field: str | None = None) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise PluginValidationError(field or key, f"{key} is required")
    return value.strip()


def _description(data: Mapping[str, Any], key: str, *, field: str | None = None) -> str:
    text = " ".join(_text(data, key, field=field).split())
    if len(text) > DESCRIPTION_MAX_CHARS:
        raise PluginValidationError(
            field or key, f"keep the description under {DESCRIPTION_MAX_CHARS} characters"
        )
    return text


def _list(data: Mapping[str, Any], key: str, *, items: type = object) -> list[Any]:
    value = data.get(key) or []
    if not isinstance(value, list) or not all(isinstance(item, items) for item in value):
        raise PluginValidationError(key, f"{key} must be a list")
    return value
