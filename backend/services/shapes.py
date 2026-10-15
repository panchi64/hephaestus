"""Narrowing for values that arrive untyped (parsed JSON, YAML, library metadata).

``isinstance(value, dict)`` leaves the key and value types unknown to the checker; these
return the same dict or list with the types a reader can rely on. Keys and items are not
inspected, so a non-string key still passes through as it was.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import cast


def as_dict(value: object) -> dict[str, object] | None:
    """``value`` when it is a dict, else None."""
    return cast("dict[str, object]", value) if isinstance(value, dict) else None


def as_list(value: object) -> list[object] | None:
    """``value`` as a list when it is a list or a tuple, else None."""
    return list(cast("Sequence[object]", value)) if isinstance(value, list | tuple) else None


def as_int(value: object) -> int | None:
    """``value`` when it is an integer (a boolean is not one), else None."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def as_number(value: object) -> float | None:
    """``value`` when it is an int or a float (a boolean is neither), else None."""
    return value if isinstance(value, int | float) and not isinstance(value, bool) else None


def as_str(value: object) -> str | None:
    """``value`` when it is a string, else None."""
    return value if isinstance(value, str) else None


def dict_list(value: object) -> list[dict[str, object]]:
    """The dict items of ``value`` when it is a list or tuple; anything else yields nothing."""
    return [item for item in map(as_dict, as_list(value) or []) if item is not None]
