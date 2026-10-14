"""``core.serde.jsonable`` — coercing tool results (including dataclasses) for the
JSON envelope. Exercised directly here; ``translate.py``/``conversation_view.py``
both rely on it to keep a tool result structured rather than falling back to a
Python repr string."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import override

from core.serde import jsonable


@dataclass(frozen=True)
class _Point:
    x: int
    y: int


def test_jsonable_passes_through_plain_json_values():
    value = {"a": 1, "b": [1, 2, "three"]}
    assert jsonable(value) == value


def test_jsonable_unwraps_a_dataclass():
    assert jsonable(_Point(x=1, y=2)) == {"x": 1, "y": 2}


def test_jsonable_unwraps_a_list_of_dataclasses():
    points = [_Point(x=1, y=2), _Point(x=3, y=4)]
    assert jsonable(points) == [{"x": 1, "y": 2}, {"x": 3, "y": 4}]


def test_jsonable_unwraps_nested_dataclasses_in_a_dict():
    assert jsonable({"point": _Point(x=1, y=2)}) == {"point": {"x": 1, "y": 2}}


def test_jsonable_stringifies_datetimes():
    ts = datetime(2026, 1, 1, tzinfo=UTC)
    assert jsonable(ts) == ts.isoformat()


def test_jsonable_falls_back_to_str_for_a_genuinely_unserializable_object():
    class Opaque:
        @override
        def __repr__(self) -> str:
            return "opaque!"

    assert jsonable(Opaque()) == "opaque!"
