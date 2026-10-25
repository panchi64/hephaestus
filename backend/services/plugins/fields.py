"""Validators for the values a ``plugin.yaml`` holds, shared by the parser and every
contribution type."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import cast

from core.exceptions import InvalidInputError, PluginValidationError
from services.egress import normalise_domain

MANIFEST_FILE = "plugin.yaml"
DESCRIPTION_MAX_CHARS = 1024

NAME = re.compile(r"[a-z][a-z0-9_]{0,39}")


def validate_name(raw: str) -> str:
    name = raw.strip().lower()
    if not NAME.fullmatch(name):
        raise PluginValidationError(
            "name", f"{raw!r} is not a plugin name: lowercase letters, digits and underscores"
        )
    return name


def domain(field: str, raw: object) -> str:
    try:
        return normalise_domain(str(raw))
    except InvalidInputError as exc:
        raise PluginValidationError(field, str(exc)) from None


def as_mapping(value: object) -> dict[str, object] | None:
    return cast("dict[str, object]", value) if isinstance(value, dict) else None


def text(data: Mapping[str, object], key: str, *, field: str | None = None) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise PluginValidationError(field or key, f"{key} is required")
    return value.strip()


def description(data: Mapping[str, object], key: str, *, field: str | None = None) -> str:
    joined = " ".join(text(data, key, field=field).split())
    if len(joined) > DESCRIPTION_MAX_CHARS:
        raise PluginValidationError(
            field or key, f"keep the description under {DESCRIPTION_MAX_CHARS} characters"
        )
    return joined


def as_list(value: object, key: str) -> list[object]:
    found: object = value or []
    if not isinstance(found, list):
        raise PluginValidationError(key, f"{key} must be a list")
    return cast("list[object]", found)


def table_list(value: object, key: str) -> list[dict[str, object]]:
    tables: list[dict[str, object]] = []
    for item in as_list(value, key):
        table = as_mapping(item)
        if table is None:
            raise PluginValidationError(key, f"{key} must be a list of mappings")
        tables.append(table)
    return tables


def in_folder(files: Mapping[str, bytes], folder: str, suffix: str) -> list[tuple[str, str, bytes]]:
    """``(path, stem, content)`` for each file directly in ``folder`` ending in ``suffix``."""
    found: list[tuple[str, str, bytes]] = []
    for path, blob in sorted(files.items()):
        parent, _, filename = path.rpartition("/")
        if parent == folder and filename.endswith(suffix):
            found.append((path, filename.removesuffix(suffix), blob))
    return found
