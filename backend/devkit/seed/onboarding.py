"""Finishes the first-run walkthrough, so a seeded instance opens on the app.

The boot passphrase creates the vault, which marks the walkthrough owed; the model
fixture has already done what the walkthrough asks for.
"""

from __future__ import annotations

from devkit.seed.base import ENDPOINT_NAME
from devkit.seed.registry import SeedContext, fixture, json_object, rows


@fixture("onboarding", order=1)
async def seed_onboarding(ctx: SeedContext) -> str:
    listed = await ctx.client.get("/models/endpoints")
    _ = listed.raise_for_status()
    endpoint = next((e for e in rows(listed) if e["name"] == ENDPOINT_NAME), None)
    if endpoint is None:
        return "walkthrough left open: no seeded endpoint"
    # Completion requires an endpoint that has passed its connection test.
    tested = await ctx.client.post(f"/models/endpoints/{endpoint['id']}/test", json={})
    _ = tested.raise_for_status()
    completed = await ctx.client.post("/onboarding/complete")
    if completed.status_code == 409:
        return f"walkthrough left open: {json_object(completed).get('detail')}"
    _ = completed.raise_for_status()
    return "walkthrough completed"
