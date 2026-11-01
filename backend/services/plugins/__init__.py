"""Plugins the model writes: contributions declared in a file bundle, run in a VM once live."""

from core.exceptions import PluginValidationError

from .calls import argument_problem, plugin_secrets, run_plugin_tool
from .manifest import (
    MANIFEST_FILE,
    PluginManifest,
    PluginSkill,
    PluginTool,
    parse_manifest,
    validate_files,
)
from .runtime import CallResult, PluginError, PluginRuntime
from .secrets import PluginSecretStore, SecretStatus, SecretValue
from .store import PluginStore, PluginView

__all__ = [
    "MANIFEST_FILE",
    "CallResult",
    "PluginError",
    "PluginManifest",
    "PluginSkill",
    "PluginRuntime",
    "PluginSecretStore",
    "PluginStore",
    "PluginTool",
    "PluginValidationError",
    "PluginView",
    "SecretStatus",
    "SecretValue",
    "argument_problem",
    "parse_manifest",
    "plugin_secrets",
    "run_plugin_tool",
    "validate_files",
]
