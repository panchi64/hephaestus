"""A plugin's ``plugin.yaml``, and the rules a plugin's files must meet.

```yaml
name: weather
description: Forecasts from Open-Meteo.
requirements: [httpx]           # pip requirement lines, installed into the plugin's venv
egress: [api.open-meteo.com]    # the only hosts its code can reach
secrets:                        # values the operator sets; the code sees a placeholder
  OPEN_METEO_KEY: api.open-meteo.com
tools: [...]                    # and any other contribution type's key
```

Everything past the runtime keys is a contribution, parsed by the type registered under
its key (``services/plugins/contributions``); some types read files instead, such as
``agents/<name>.md``. A secret's value never reaches the plugin: its environment variable
holds a placeholder that smol's ``CredentialSpec`` swaps for the value, in requests to the
secret's own host only.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import cast

import yaml

from core.exceptions import PluginValidationError
from services.agents.definition import AgentDefinition

from .contributions.agents import AGENTS
from .contributions.base import BundleSource, Described
from .contributions.registry import load_types
from .contributions.skills import SKILLS, PluginSkill
from .contributions.tools import TOOLS, PluginTool
from .fields import (
    MANIFEST_FILE,
    as_list,
    as_mapping,
    description,
    domain,
    text,
    validate_name,
)

__all__ = [
    "BUNDLE_MAX_BYTES",
    "MANIFEST_FILE",
    "PluginManifest",
    "PluginSkill",
    "PluginTool",
    "parse_manifest",
    "secret_domain",
    "validate_files",
    "validate_name",
    "validate_path",
]

BUNDLE_MAX_BYTES = 1024 * 1024
BUNDLE_MAX_FILES = 200

#: Keys that configure the plugin's VM rather than contribute to the platform.
_RUNTIME_KEYS = frozenset({"name", "description", "requirements", "egress", "secrets"})
_SECRET = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
_REQUIREMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\-\[\],<>=!~ ;'\"]*")


@dataclass(frozen=True)
class PluginManifest:
    name: str
    description: str
    requirements: tuple[str, ...] = ()
    egress: tuple[str, ...] = ()
    #: Secret name → the host its value may be sent to.
    secrets: Mapping[str, str] = field(default_factory=lambda: {})
    #: Contribution type key → its items, for every type the plugin uses.
    contributions: Mapping[str, tuple[object, ...]] = field(default_factory=lambda: {})

    @property
    def tools(self) -> tuple[PluginTool, ...]:
        return TOOLS.of(self.contributions)

    @property
    def agents(self) -> tuple[AgentDefinition, ...]:
        return AGENTS.of(self.contributions)

    @property
    def skills(self) -> tuple[PluginSkill, ...]:
        return SKILLS.of(self.contributions)

    def tool(self, name: str) -> PluginTool | None:
        return next((tool for tool in self.tools if tool.name == name), None)

    def described(self) -> dict[str, list[Described]]:
        """Every contribution as the Plugins screen lists it, by type key."""
        registry = load_types()
        found: dict[str, list[Described]] = {}
        for key, items in self.contributions.items():
            kind = registry.get(key)
            if kind is not None and items:
                found[key] = [kind.describe(item) for item in items]
        return found


def validate_files(files: Mapping[str, bytes]) -> PluginManifest:
    """The bundle's manifest, once every path and the total size are within bounds."""
    if len(files) > BUNDLE_MAX_FILES:
        raise PluginValidationError("files", f"a plugin holds at most {BUNDLE_MAX_FILES} files")
    if sum(len(blob) for blob in files.values()) > BUNDLE_MAX_BYTES:
        raise PluginValidationError(
            "files", f"a plugin is at most {BUNDLE_MAX_BYTES // 1024} KB of files"
        )
    for path in files:
        _ = validate_path(path)
    raw = files.get(MANIFEST_FILE)
    if raw is None:
        raise PluginValidationError("files", f"a plugin needs a {MANIFEST_FILE}")
    try:
        manifest_text = raw.decode()
    except UnicodeDecodeError:
        raise PluginValidationError(MANIFEST_FILE, "is not UTF-8 text") from None
    manifest = parse_manifest(manifest_text, files)
    if not any(manifest.contributions.values()):
        offered = ", ".join(_sandboxed_keys())
        raise PluginValidationError("tools", f"a plugin contributes at least one of: {offered}")
    return manifest


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


def parse_manifest(manifest_text: str, files: Mapping[str, bytes] | None = None) -> PluginManifest:
    """The manifest, with its contributions parsed. Without ``files`` only the text is
    checked: file-based contributions come out empty and file references unchecked."""
    try:
        loaded = cast("object", yaml.safe_load(manifest_text))
    except yaml.YAMLError as exc:
        raise PluginValidationError(MANIFEST_FILE, f"is not valid YAML: {exc}") from None
    data = as_mapping(loaded)
    if data is None:
        raise PluginValidationError(MANIFEST_FILE, "must be a mapping")
    registry = load_types()
    unknown = set(data) - _RUNTIME_KEYS - set(registry.keys())
    if unknown:
        raise PluginValidationError(
            MANIFEST_FILE,
            f"unknown key {sorted(unknown)[0]!r}; a plugin may use "
            + ", ".join(sorted(_RUNTIME_KEYS | set(_sandboxed_keys()))),
        )
    name = validate_name(text(data, "name"))
    contributions: dict[str, tuple[object, ...]] = {}
    for kind in registry:
        declared = kind.key in data
        if "sandboxed" not in kind.tiers:
            if declared:
                raise PluginValidationError(
                    kind.key, f"only built-in plugins contribute {kind.label.lower()}"
                )
            continue
        items = kind.parse(BundleSource(name, data.get(kind.key), declared, files))
        if items:
            contributions[kind.key] = items
    return PluginManifest(
        name=name,
        description=description(data, "description"),
        requirements=tuple(
            _requirement(line) for line in as_list(data.get("requirements"), "requirements")
        ),
        egress=tuple(
            sorted({domain("egress", raw) for raw in as_list(data.get("egress"), "egress")})
        ),
        secrets=_secrets(data.get("secrets") or {}),
        contributions=contributions,
    )


def _sandboxed_keys() -> list[str]:
    return [kind.key for kind in load_types() if "sandboxed" in kind.tiers]


def _secrets(raw_secrets: object) -> dict[str, str]:
    if not isinstance(raw_secrets, dict):
        raise PluginValidationError("secrets", "secrets map each name to the host it is for")
    raw = cast("dict[object, object]", raw_secrets)
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
    found = domain("secrets", raw)
    if found.startswith("*."):
        raise PluginValidationError(
            "secrets", f"{raw!r}: a secret goes to one host, not a wildcard"
        )
    return found


def _requirement(raw: object) -> str:
    line = str(raw).strip()
    if not _REQUIREMENT.fullmatch(line) or line.startswith("-"):
        raise PluginValidationError("requirements", f"{raw!r} is not a requirement line")
    return line
