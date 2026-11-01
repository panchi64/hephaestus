"""Shared serialization helpers.

Lives in ``core`` so both the live translator (``agent/translate.py``) and the
static-history projection (``services/conversation_view.py``) can share the one
coercion without the lower layer importing the orchestrator — and, for ``as_utc``,
so every service that reads a timestamp back out of the DB normalizes it the same
way instead of carrying a private copy.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from typing import cast, overload

from pydantic import JsonValue, TypeAdapter

logger = logging.getLogger(__name__)

_JSON = TypeAdapter(JsonValue)


def parse_json(text: str | bytes) -> JsonValue:
    """The JSON document in ``text``; malformed input raises ``ValueError``."""
    # `json.loads` only builds JSON values, and it accepts what `validate_json` refuses:
    # a BOM, UTF-16 bytes, a lone surrogate escape, deep nesting.
    return cast("JsonValue", json.loads(text))


def to_json(value: object) -> JsonValue:
    """``value`` as JSON data; anything JSON cannot carry raises ``ValueError``."""
    return _JSON.validate_python(value)


def coerce_int(value: JsonValue) -> int:
    """``int(value)``; raises ``TypeError`` for null, arrays and objects."""
    if isinstance(value, bool | int | float | str):
        return int(value)
    raise TypeError(f"not a number: {value!r}")


@overload
def as_utc(value: datetime) -> datetime: ...
@overload
def as_utc(value: None) -> None: ...


def as_utc(value: datetime | None) -> datetime | None:
    """A datetime forced onto tz-aware UTC; ``None`` passes through.

    SQLite has no native datetime type, so the dialect round-trips a tz-aware value
    as a **naive** one — a row read back and compared or sorted against a fresh
    `utcnow()` raises ``TypeError: can't compare offset-naive and offset-aware
    datetimes``. A naive value is therefore *assumed* UTC and re-stamped; an aware
    one is converted, so a caller-supplied timestamp in any zone (a query-string
    filter, an ICS import) lands on the same scale as the stored ones.
    """
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _structure(value: object) -> JsonValue:
    """Recursively unwrap dataclasses/datetimes into plain JSON-able structures.
    A tool may return a dataclass (or a list/dict of them) — e.g. `SearchResult` —
    and losing that structure to `str(value)` would make the persisted result
    unusable to any downstream reader (the frontend's cold-reload citation
    derivation among them)."""
    if is_dataclass(value) and not isinstance(value, type):
        return _structure(asdict(value))
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _structure(v) for k, v in cast("dict[str, object]", value).items()}
    if isinstance(value, list | tuple):
        return [_structure(v) for v in cast("Sequence[object]", value)]
    # Anything else passes through as is; `jsonable` rejects what `json.dumps` cannot carry.
    return cast("JsonValue", value)


def jsonable(value: object) -> JsonValue:
    """Coerce a tool result into something the JSON envelope can carry.

    Never raises: the whole coercion — the ``_structure`` pre-pass included — sits under
    the guard, so a value that can't be structured or serialized (a reference cycle blowing
    the recursion limit, a non-deepcopyable dataclass field) degrades to ``str(value)``
    rather than crashing tool-result serialization for the turn."""
    try:
        structured = _structure(value)
        _ = json.dumps(structured)
        return structured
    except Exception:  # total fallback: any coercion failure degrades to str
        logger.debug("jsonable: %s degraded to str", type(value).__name__, exc_info=True)
        return str(value)
