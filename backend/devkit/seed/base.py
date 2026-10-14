"""The one fixture everything else depends on: a model the instance can actually use.

An instance with no model bound is not merely emptier — most of the product does not
work. Chat cannot answer, titles cannot be generated, memories cannot be embedded, and
the surfaces that would show a change render an error instead. So this runs first, and
everything after it can assume a working ``main``.

By default that is the stub. When ``HEPH_DEV_CHAT_*`` names a real endpoint, this binds
that instead — same seam, same registry row, so nothing downstream knows the difference.
"""

from __future__ import annotations

from devkit import live
from devkit.seed.registry import SeedContext, fixture, json_object, rows
from devkit.stub.app import MODEL_ID

#: The registry rows this fixture owns. Named rather than generated so re-seeding an
#: existing workspace updates them in place instead of accumulating duplicates.
ENDPOINT_NAME = "dev-instance"

#: Every role the registry knows (``services.llm.ROLES``), all pointed at the one
#: endpoint. `main` drives chat; leaving the other two unbound would make a seeded
#: workspace fail at exactly the background work — titling, compaction, recall — that a
#: visual check is least likely to notice going missing.
ROLES = ("main", "utility", "embedding")

#: Declared rather than discovered, and not optional. The app refuses to send a turn on
#: an endpoint whose context window it does not know — it cannot keep a conversation
#: inside a bound it has not been told — and the stub has no window to report. Without
#: this the instance seeds cleanly and then answers every message with a settings error.
#: Large enough that auto-compaction never fires in a seeded thread by accident.
CONTEXT_WINDOW = 128_000


async def _endpoint_id(ctx: SeedContext) -> str:
    """Create the endpoint, or find the one a previous seed made."""
    if live.configured():
        spec = live.endpoint()
        body = {
            "name": ENDPOINT_NAME,
            "base_url": spec["base_url"],
            "model": spec["model"],
            "api_key": spec["api_key"],
        }
    else:
        # No key: the stub ignores auth, as a local engine does. The adapter substitutes
        # a placeholder the OpenAI client insists on, so nothing here has to.
        body = {
            "name": ENDPOINT_NAME,
            "base_url": ctx.instance.stub_url,
            "model": MODEL_ID,
            "api_key": None,
        }

    full = {**body, "native_tools": True, "context_window": CONTEXT_WINDOW}

    # Look before creating. Endpoint names are unique per operator, and re-seeding an
    # existing workspace is the ordinary path after a version bump — so the second run
    # must find the row rather than collide with it. Reading first, instead of treating
    # the collision as the signal: the constraint surfaces as a server error, and an
    # error is a bad thing to build control flow on even when it is reliable.
    listed = await ctx.client.get("/models/endpoints")
    _ = listed.raise_for_status()
    for endpoint in rows(listed):
        if endpoint["name"] == ENDPOINT_NAME:
            # Updated rather than left alone, so a workspace seeded by an older pack
            # picks up a field that pack did not set.
            updated = await ctx.client.patch(f"/models/endpoints/{endpoint['id']}", json=full)
            _ = updated.raise_for_status()
            return str(endpoint["id"])

    created = await ctx.client.post("/models/endpoints", json=full)
    _ = created.raise_for_status()
    return str(json_object(created)["id"])


@fixture("model", order=0)
async def seed_model(ctx: SeedContext) -> str:
    endpoint_id = await _endpoint_id(ctx)
    model = live.endpoint()["model"] if live.configured() else MODEL_ID
    for role in ROLES:
        bound = await ctx.client.put(
            f"/models/roles/{role}", json={"endpoint_ids": [endpoint_id], "model": model}
        )
        _ = bound.raise_for_status()
    where = "a real endpoint (HEPH_DEV_CHAT_*)" if live.configured() else "the stub"
    return f"{model} on {where}, bound to {', '.join(ROLES)}"
