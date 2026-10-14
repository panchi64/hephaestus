"""How a fixture registers, and how the pack knows it has changed.

Fixtures write **through the HTTP API**, never into the stores directly. Direct writes
would skip vault sealing, the ownership checks, the conversation write-behind drainer and
the embed-on-persist path — so the rows would not be shaped like real rows, and a surface
that renders them proves nothing about the surface that renders the operator's. It is
slower and it is the point.

The version is **derived, never declared**. A hand-maintained number is a number somebody
forgets, and a forgotten bump means a stale workspace silently disagreeing with the code
— which a future session then debugs as a ghost. Hashing the fixture sources and the
migration set means adding a fixture or a migration re-seeds on the next ``up`` without
anyone deciding to.
"""

from __future__ import annotations

import hashlib
import importlib
import pkgutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from devkit.instance import DevInstance

_SEED_DIR = Path(__file__).resolve().parent
_MIGRATIONS = _SEED_DIR.parent.parent / "migrations" / "versions"


@dataclass(frozen=True, slots=True)
class SeedContext:
    """What a fixture is given: a client already pointed at the instance, and the
    instance itself (for the stub's URL, and for writing files it needs on disk)."""

    client: httpx.AsyncClient
    instance: DevInstance


#: A fixture returns a one-line summary of what it made, which is what ``up`` prints.
Fixture = Callable[[SeedContext], Awaitable[str]]

_REGISTERED: list[tuple[int, str, Fixture]] = []


def fixture(name: str, *, order: int) -> Callable[[Fixture], Fixture]:
    """Register a fixture. ``order`` is a dependency ordering, not a priority — the base
    fixture must bind a model before anything drives a conversation through one."""

    def register(function: Fixture) -> Fixture:
        _REGISTERED.append((order, name, function))
        return function

    return register


def rows(response: httpx.Response) -> list[dict[str, Any]]:
    """The rows in a listing, whichever shape the surface returns.

    Some listings are a bare JSON array and others wrap one in ``items`` — a difference
    that is invisible until a fixture indexes into the wrong one and fails with a
    ``KeyError: 0``. Absorbed here so a fixture author meets it once, in this docstring,
    rather than once per surface. A non-200 reads as empty: every fixture uses this to
    decide whether it has work to do, and a feature that is switched off should leave
    seeding to carry on rather than take it down.
    """
    if response.status_code != 200:
        return []
    try:
        body = response.json()
    except ValueError:
        return []
    if isinstance(body, dict):
        body = body.get("items", [])
    return body if isinstance(body, list) else []


def _import_fixtures() -> None:
    """Import every fixture module, so a module's presence in the pack is its registration."""
    for info in pkgutil.iter_modules([str(_SEED_DIR)]):
        if info.name != "registry" and not info.name.startswith("_"):
            _ = importlib.import_module(f"devkit.seed.{info.name}")


def registered() -> list[tuple[int, str, Fixture]]:
    """Every fixture, in the order they run."""
    _import_fixtures()
    return sorted(_REGISTERED, key=lambda entry: (entry[0], entry[1]))


def seed_version() -> str:
    """A short hash over everything that decides what a seeded workspace contains.

    The migration *filenames* rather than their contents: a revision is immutable once
    written, so the set of them identifies a schema exactly, and reading every file to
    learn what reading their names already says would only be slower.
    """
    digest = hashlib.blake2b(digest_size=6)
    for path in sorted(_SEED_DIR.glob("*.py")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    for path in sorted(_MIGRATIONS.glob("*.py")):
        digest.update(path.name.encode())
    return digest.hexdigest()


async def run_seed(ctx: SeedContext) -> list[str]:
    """Run every registered fixture in order, returning what each reported."""
    summaries = []
    for _order, name, function in registered():
        summaries.append(f"{name}: {await function(ctx)}")
    return summaries
