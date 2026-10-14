"""The fixture pack, run against a real app.

Not against a real *instance*: ``client_app`` boots the same application over an
in-memory database and an ASGI transport, which exercises every route the fixtures call
without a port, a process or a workspace on disk. What it cannot host is the stub — the
seeded endpoint points at an ``http://`` URL that nothing answers here — so the fixture
that drives real conversations is left to the end-to-end path and everything else is
covered here.

The contract worth guarding is the registration one: a fixture is a module plus a line,
the pack's version moves on its own, and a re-run does not double what it made. Those are
the properties a future session relies on without ever reading this package.
"""

from __future__ import annotations

import socket
import threading
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
import uvicorn
from pydantic import JsonValue

from devkit import ports
from devkit.instance import DevInstance
from devkit.seed import registry, seed_version
from devkit.seed.base import ENDPOINT_NAME, ROLES, seed_model
from devkit.seed.surfaces import seed_calendar, seed_memory, seed_tasks
from devkit.stub import create_stub

from ._helpers import client_app
from ._json import json_body, json_list, n, obj


@pytest.fixture
def served_stub() -> Iterator[int]:
    """A real stub, listening on a real port, on a slot this test has claimed.

    Load-bearing rather than scene-setting: binding the ``embedding`` role *probes* the
    endpoint over HTTP before it will save the binding, so the base fixture cannot be
    exercised against a URL nothing answers. Before this existed the test passed only
    because a dev instance happened to be running on that port — it borrowed a live
    process, and failed the moment anyone stopped it.

    The socket is bound here rather than left to uvicorn because binding *is* the claim:
    two parallel workers racing for a slot cannot both succeed, and the loser moves on.
    """
    for slot in range(ports.MAX_SLOTS):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", ports.slot_ports(slot)[2]))
        except OSError:
            sock.close()
            continue
        sock.listen(128)
        break
    else:
        pytest.skip("no free dev-instance stub port on this host")

    server = uvicorn.Server(uvicorn.Config(create_stub(), log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        while not server.started and thread.is_alive():
            _ = threading.Event().wait(0.02)
        yield slot
    finally:
        server.should_exit = True
        thread.join(timeout=10.0)
        sock.close()


@pytest.fixture
def instance(tmp_path: Path, served_stub: int) -> DevInstance:
    return DevInstance(name="test", slot=served_stub, root=tmp_path, password="unused-here")


@pytest.fixture
async def seeding(instance: DevInstance) -> AsyncIterator[registry.SeedContext]:
    async with client_app() as (client, _app):
        yield registry.SeedContext(client=client, instance=instance)


async def test_the_base_fixture_binds_every_role_to_one_endpoint(seeding: registry.SeedContext):
    # Nothing else in the pack can run without this: an instance with no model bound
    # does not merely look emptier, most of the product returns an error instead.
    summary = await seed_model(seeding)
    assert "hephaestus-stub" in summary

    endpoints = [obj(e) for e in json_list(await seeding.client.get("/models/endpoints"))]
    assert [e["name"] for e in endpoints] == [ENDPOINT_NAME]
    # Declared rather than discovered: the app refuses to send a turn on an endpoint
    # whose context window it does not know, and the stub has none to report.
    assert n(endpoints[0]["context_window"]) > 0

    roles = json_body(await seeding.client.get("/models/roles"))
    for role in ROLES:
        assert obj(roles[role])["endpoint_ids"] == [endpoints[0]["id"]]


async def test_seeding_twice_updates_rather_than_duplicates(seeding: registry.SeedContext):
    _ = await seed_model(seeding)
    _ = await seed_model(seeding)
    endpoints = json_list(await seeding.client.get("/models/endpoints"))
    assert len(endpoints) == 1


async def test_every_surface_fixture_populates_and_then_leaves_itself_alone(
    seeding: registry.SeedContext,
):
    for populate in (seed_calendar, seed_memory, seed_tasks):
        first = await populate(seeding)
        assert "could not" not in first, first
        # Re-seeding happens on every version bump, so a pack that doubled its own rows
        # each time would turn a bump into a slowly growing pile.
        assert "already populated" in await populate(seeding)

    assert len(registry.rows(await seeding.client.get("/calendar/events"))) == 3
    assert len(registry.rows(await seeding.client.get("/memory"))) == 3


async def test_a_seeded_task_never_fires_on_its_own(seeding: registry.SeedContext):
    # It would spend model calls on a schedule nobody set, in a workspace nobody is
    # watching — and real money, against an endpoint named by HEPH_DEV_CHAT_*.
    _ = await seed_tasks(seeding)
    tasks = registry.rows(await seeding.client.get("/tasks"))
    assert tasks and all(task["enabled"] is False for task in tasks)


def test_the_pack_runs_base_before_anything_that_needs_a_model():
    order = [name for _order, name, _fn in registry.registered()]
    assert order[0] == "model"
    assert "conversations" in order


def test_the_version_moves_when_a_fixture_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # Derived, never declared: a hand-maintained number is one somebody forgets, and a
    # forgotten bump leaves a stale workspace disagreeing with the code in silence.
    before = seed_version()
    assert before == seed_version()

    pack = tmp_path / "seed"
    pack.mkdir()
    _ = (pack / "existing.py").write_text("# a fixture\n")
    monkeypatch.setattr(registry, "_SEED_DIR", pack)
    monkeypatch.setattr(registry, "_MIGRATIONS", tmp_path / "versions")
    with_one = seed_version()
    _ = (pack / "another.py").write_text("# a second fixture\n")
    assert seed_version() != with_one


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ([{"id": "a"}], 1),
        ({"items": [{"id": "a"}, {"id": "b"}]}, 2),
        ({"items": []}, 0),
        ({"unexpected": "shape"}, 0),
    ],
)
def test_listings_are_read_whichever_shape_they_come_in(body: JsonValue, expected: int):
    # Some listings are a bare array and others wrap one in `items`. A fixture that
    # indexed into the wrong one fails with `KeyError: 0`, which says nothing.
    response = httpx.Response(200, json=body)
    assert len(registry.rows(response)) == expected


def test_a_listing_that_failed_reads_as_empty():
    # Fixtures use this to decide whether they have work to do. A feature switched off
    # should leave seeding to carry on rather than take it down.
    assert registry.rows(httpx.Response(404, json={"detail": "no"})) == []
