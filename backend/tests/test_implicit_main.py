"""What `main` resolves to when the operator has bound nothing.

An unbound `main` used to be a degraded capability, which made a fresh workspace refuse
every turn. That was defensible when the picker also showed nothing — but the picker
resolves a *display* fallback ("first available model") of its own, so the composer
showed a model, SEND refused it, and re-picking that exact model in the dropdown was the
only way to make the two agree. The display was a fiction; nothing had been bound.

So `main` now resolves to the first usable endpoint and model when nothing is bound, and
says so: the roles listing reports what it resolved to with `implicit` set, and the
picker shows *that* rather than guessing. Display and execution come from one answer.

It is a default and never a pin — recomputed, never written — which is what these tests
pin down: adding a better endpoint has to move it, and an explicit choice has to win and
stop being implicit.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from core.db import init_db, make_engine
from core.exceptions import DegradedCapabilityError
from core.vault import Vault
from services.registry import ModelRegistry
from tests._helpers import client_app
from tests._json import json_body, obj

OWNER = "op"


async def _registry() -> ModelRegistry:
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    tmp = tempfile.mkdtemp()
    vault = Vault(Path(tmp) / "keyfile.json")
    await vault.setup("pw")
    return ModelRegistry(engine, vault)


async def test_unbound_main_resolves_to_the_only_usable_endpoint():
    reg = await _registry()
    ep = await reg.create_endpoint(OWNER, name="local", base_url="http://x/v1", model="qwen")

    assert await reg.implicit_main_binding(OWNER) == ([ep.id], "qwen")
    # And the resolution path agrees — the point of the whole change is that the turn
    # this describes actually runs.
    resolved = await reg.resolve_detailed("main", owner_id=OWNER)
    assert resolved.model is not None


async def test_nothing_usable_is_still_degraded():
    # The gate must keep firing on a genuinely empty workspace: an implicit default is
    # for "you have one and haven't picked it", not for "you have none".
    reg = await _registry()
    assert await reg.implicit_main_binding(OWNER) is None
    try:
        _ = await reg.resolve_detailed("main", owner_id=OWNER)
    except DegradedCapabilityError:
        pass
    else:
        raise AssertionError("an empty workspace must not resolve a model")


async def test_a_disabled_endpoint_is_never_the_default():
    reg = await _registry()
    _ = await reg.create_endpoint(
        OWNER, name="aaa benched", base_url="http://x/v1", model="qwen", enabled=False
    )
    live = await reg.create_endpoint(OWNER, name="zzz live", base_url="http://y/v1", model="llama")

    # Ordered by name, so the benched one is scanned first — this fails if the scan
    # takes the first endpoint rather than the first *usable* one.
    assert await reg.implicit_main_binding(OWNER) == ([live.id], "llama")


async def test_the_default_follows_the_catalog_rather_than_insertion_order():
    # Endpoints are scanned in `list_endpoints` order (by name), so the same catalog
    # always yields the same default regardless of the sequence it was built in.
    reg = await _registry()
    _ = await reg.create_endpoint(OWNER, name="zulu", base_url="http://z/v1", model="z-model")
    alpha = await reg.create_endpoint(OWNER, name="alpha", base_url="http://a/v1", model="a-model")

    assert await reg.implicit_main_binding(OWNER) == ([alpha.id], "a-model")


async def test_a_new_endpoint_can_become_the_default():
    # The memo must not outlive the catalog it was computed from. This is the case that
    # made a cache worth invalidating explicitly: the first answer is "none".
    reg = await _registry()
    assert await reg.implicit_main_binding(OWNER) is None

    ep = await reg.create_endpoint(OWNER, name="local", base_url="http://x/v1", model="qwen")
    assert await reg.implicit_main_binding(OWNER) == ([ep.id], "qwen")


async def test_deleting_the_default_moves_it_on():
    reg = await _registry()
    first = await reg.create_endpoint(OWNER, name="aaa", base_url="http://a/v1", model="a-model")
    second = await reg.create_endpoint(OWNER, name="bbb", base_url="http://b/v1", model="b-model")
    assert await reg.implicit_main_binding(OWNER) == ([first.id], "a-model")

    await reg.delete_endpoint(OWNER, first.id)
    # A stale id here would be worse than no default: resolution would trip on it.
    assert await reg.implicit_main_binding(OWNER) == ([second.id], "b-model")


async def test_benching_the_default_moves_it_on():
    reg = await _registry()
    first = await reg.create_endpoint(OWNER, name="aaa", base_url="http://a/v1", model="a-model")
    second = await reg.create_endpoint(OWNER, name="bbb", base_url="http://b/v1", model="b-model")
    assert await reg.implicit_main_binding(OWNER) == ([first.id], "a-model")

    _ = await reg.update_endpoint(OWNER, first.id, enabled=False)
    assert await reg.implicit_main_binding(OWNER) == ([second.id], "b-model")


async def test_an_explicit_binding_wins_over_the_default():
    reg = await _registry()
    _ = await reg.create_endpoint(
        OWNER, name="aaa default", base_url="http://a/v1", model="a-model"
    )
    chosen = await reg.create_endpoint(OWNER, name="zzz chosen", base_url="http://z/v1", model="z")
    await reg.set_role(OWNER, "main", [chosen.id], model="z")

    # The operator's pick is what resolves — the implicit rule only fills a vacuum.
    chain, pinned = await reg.get_role_binding(OWNER, "main")
    assert chain == [chosen.id]
    assert pinned == "z"


async def test_the_default_is_never_written_down():
    # The whole distinction between a default and a pin. If resolving stored a binding,
    # adding a better endpoint later would leave `main` fastened to whichever happened
    # to exist first — and the operator would never have chosen it.
    reg = await _registry()
    _ = await reg.create_endpoint(OWNER, name="local", base_url="http://x/v1", model="qwen")

    _ = await reg.resolve_detailed("main", owner_id=OWNER)

    assert await reg.get_role_binding(OWNER, "main") == ([], None)
    assert await reg.list_roles(OWNER) == {}


# ── The roles listing, which is what the picker actually reads ───────────────────


async def test_roles_listing_reports_the_implicit_main_and_flags_it():
    """`list_roles` returns only *stored* bindings, so an unbound `main` was absent from
    this payload entirely — and the picker, which reads it, had nothing to show for the
    model a turn would run on. That absence is what left it guessing."""
    async with client_app() as (client, _app):
        created = await client.post(
            "/models/endpoints",
            json={"name": "local", "base_url": "http://x/v1", "model": "qwen"},
        )
        assert created.status_code in (200, 201), created.text
        endpoint_id = json_body(created)["id"]

        roles = json_body(await client.get("/models/roles"))
        assert "main" in roles, "an unbound main with a usable model must still be reported"
        main = obj(roles["main"])
        assert main["endpoint_ids"] == [endpoint_id]
        assert main["model"] == "qwen"
        # The flag is the whole reason this is safe to show: a surface can say
        # "defaulting to this" instead of claiming the operator chose it.
        assert main["implicit"] is True


async def test_an_explicit_pick_stops_being_implicit():
    async with client_app() as (client, _app):
        created = await client.post(
            "/models/endpoints",
            json={"name": "local", "base_url": "http://x/v1", "model": "qwen"},
        )
        endpoint_id = json_body(created)["id"]

        put = await client.put(
            "/models/roles/main", json={"endpoint_ids": [endpoint_id], "model": "qwen"}
        )
        assert put.status_code == 204, put.text

        main = obj(json_body(await client.get("/models/roles"))["main"])
        assert main["endpoint_ids"] == [endpoint_id]
        assert main["implicit"] is False


async def test_an_empty_workspace_reports_no_main_at_all():
    # Nothing to default to, so nothing is claimed — the composer's gate stays on, and
    # the surfacing that actually helps ("add an endpoint") is not talked over.
    async with client_app() as (client, _app):
        roles = json_body(await client.get("/models/roles"))
        assert "main" not in roles
