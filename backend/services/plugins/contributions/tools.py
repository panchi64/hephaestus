"""``tools``: functions the agent can call, run in the plugin's VM.

```yaml
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
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import override

from jsonschema import Draft202012Validator, SchemaError

from core.exceptions import PluginValidationError
from services.plugins.fields import NAME, as_mapping, description, table_list, text
from services.tool_sensitivity import Sensitivity

from .base import BundleSource, Described, TypedContribution
from .registry import CONTRIBUTIONS

TOOLS_MAX = 32
#: A tool is offered as ``plugins_<plugin>_<tool>``, and providers cap a tool name at 64.
_TOOL_NAME_MAX = 64 - len("plugins__")
_ENTRY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*:[A-Za-z_][A-Za-z0-9_]*")
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
    parameters: dict[str, object]
    sensitivity: Sensitivity

    @property
    def module(self) -> str:
        return self.entry.partition(":")[0]

    @property
    def function(self) -> str:
        return self.entry.partition(":")[2]


class Tools(TypedContribution[PluginTool]):
    @override
    def parse(self, source: BundleSource) -> tuple[PluginTool, ...]:
        raw_tools = table_list(source.raw, self.key)
        if len(raw_tools) > TOOLS_MAX:
            raise PluginValidationError("tools", f"a plugin contributes at most {TOOLS_MAX} tools")
        tools = tuple(_tool(raw) for raw in raw_tools)
        names = [tool.name for tool in tools]
        if len(set(names)) != len(names):
            raise PluginValidationError("tools", "two tools share a name")
        for tool in tools:
            if len(source.plugin_name) + len(tool.name) > _TOOL_NAME_MAX:
                raise PluginValidationError(
                    "tools",
                    f"{source.plugin_name}_{tool.name} is too long a tool name: "
                    + "shorten one of the two",
                )
            if source.files is not None:
                module = tool.module.replace(".", "/")
                if f"{module}.py" not in source.files and f"{module}/__init__.py" not in (
                    source.files
                ):
                    raise PluginValidationError(
                        "tools",
                        f"tool {tool.name!r} names module {tool.module!r}, "
                        + "which is not in the files",
                    )
        return tools

    @override
    def describe_one(self, item: PluginTool) -> Described:
        return {
            "name": item.name,
            "description": item.description,
            "sensitivity": item.sensitivity.value,
        }


def _tool(raw: dict[str, object]) -> PluginTool:
    unknown = set(raw) - {"name", "description", "entry", "parameters", "sensitivity"}
    if unknown:
        raise PluginValidationError("tools", f"unknown tool key {sorted(unknown)[0]!r}")
    name = text(raw, "name", field="tools")
    if not NAME.fullmatch(name):
        raise PluginValidationError(
            "tools", f"{name!r} is not a tool name: lowercase letters, digits and underscores"
        )
    entry = text(raw, "entry", field="tools")
    if not _ENTRY.fullmatch(entry):
        raise PluginValidationError("tools", f"{entry!r} is not a `module:function` entry")
    parameters = as_mapping(raw.get("parameters") or {"type": "object", "properties": {}})
    if parameters is None or parameters.get("type") != "object":
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
        name, description(raw, "description", field="tools"), entry, parameters, sensitivity
    )


TOOLS = CONTRIBUTIONS.register(Tools("tools", "Tools", PluginTool))
