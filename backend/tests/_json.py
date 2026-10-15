"""Typed reads of JSON off the wire: each helper asserts the shape it returns."""

from __future__ import annotations

import httpx
from pydantic import BaseModel, JsonValue, TypeAdapter

from core.serde import parse_json

__all__ = ["JSON", "arr", "b", "dump", "json_body", "json_list", "n", "obj", "parse_json", "s"]

JSON = TypeAdapter(JsonValue)


def obj(v: JsonValue) -> dict[str, JsonValue]:
    """``v`` as an object."""
    assert isinstance(v, dict), f"expected a JSON object, got {v!r}"
    return v


def arr(v: JsonValue) -> list[JsonValue]:
    """``v`` as an array."""
    assert isinstance(v, list), f"expected a JSON array, got {v!r}"
    return v


def s(v: JsonValue) -> str:
    """``v`` as a string."""
    assert isinstance(v, str), f"expected a JSON string, got {v!r}"
    return v


def n(v: JsonValue) -> int | float:
    """``v`` as a number; a boolean is not one."""
    assert isinstance(v, int | float) and not isinstance(v, bool), (
        f"expected a JSON number, got {v!r}"
    )
    return v


def b(v: JsonValue) -> bool:
    """``v`` as a boolean."""
    assert isinstance(v, bool), f"expected a JSON boolean, got {v!r}"
    return v


def dump(model: BaseModel) -> dict[str, JsonValue]:
    """``model`` as a JSON object, so an ``Any`` field is read as ``JsonValue``."""
    return obj(parse_json(model.model_dump_json()))


def json_body(resp: httpx.Response) -> dict[str, JsonValue]:
    """The response body as a JSON object."""
    return obj(parse_json(resp.content))


def json_list(resp: httpx.Response) -> list[JsonValue]:
    """The response body as a JSON array."""
    return arr(parse_json(resp.content))
