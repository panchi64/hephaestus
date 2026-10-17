"""The rest of the surfaces: calendar, memory, tasks, projects, corpus.

One module rather than five, because each is the same three lines against a different
route and five files repeating that shape would be five places to keep a convention. If
any one of these grows a real setup — a corpus that needs documents written first, a
project that needs a repository — it earns its own file then.

Every fixture here is **idempotent by inspection**: it asks what is already there and
does nothing if the surface is populated. Re-seeding happens whenever the fixture pack
changes, and a pack that doubled its own rows each time would turn a version bump into a
slowly growing pile.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from devkit.seed.registry import SeedContext, fixture, json_object, rows


def _soon(days: int, hour: int) -> str:
    """A local-time stamp relative to now, so seeded events never age into the past."""
    when = (datetime.now() + timedelta(days=days)).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )
    return when.isoformat()


@fixture("calendar", order=20)
async def seed_calendar(ctx: SeedContext) -> str:
    calendars = rows(await ctx.client.get("/calendar/calendars"))
    if not calendars:
        created = await ctx.client.post("/calendar/calendars", json={"name": "Personal"})
        if created.status_code not in (200, 201):
            return "could not create a calendar"
        calendar_id = str(json_object(created)["id"])
    else:
        calendar_id = str(calendars[0]["id"])

    if rows(await ctx.client.get("/calendar/events")):
        return "already populated"

    events = [
        ("Design review", 1, 10),
        ("Lunch with Sam", 2, 13),
        ("Ship the release", 5, 16),
    ]
    made = 0
    for title, days, hour in events:
        created = await ctx.client.post(
            "/calendar/events",
            json={
                "calendar_id": calendar_id,
                "title": title,
                "start": _soon(days, hour),
                "end": _soon(days, hour + 1),
            },
        )
        made += created.status_code in (200, 201)
    return f"1 calendar, {made} events"


@fixture("memory", order=30)
async def seed_memory(ctx: SeedContext) -> str:
    if rows(await ctx.client.get("/memory")):
        return "already populated"
    memories = [
        (
            "Prefers concise answers with the reasoning shown only when it changes the result.",
            "How the operator likes answers",
            True,
        ),
        (
            "Works in Python and TypeScript; uses uv and bun rather than pip and npm.",
            "The operator's languages and package managers",
            False,
        ),
        ("Is in the Europe/Madrid timezone.", "The operator's timezone", False),
    ]
    made = 0
    for content, description, pinned in memories:
        created = await ctx.client.post(
            "/memory", json={"content": content, "description": description, "pinned": pinned}
        )
        made += created.status_code in (200, 201)
    return f"{made} memories"


@fixture("tasks", order=40)
async def seed_tasks(ctx: SeedContext) -> str:
    if rows(await ctx.client.get("/tasks")):
        return "already populated"
    created = await ctx.client.post(
        "/tasks",
        json={
            "kind": "agent",
            "title": "Morning briefing",
            "prompt": "Summarise what is on today and anything waiting on me.",
            # Disabled: a seeded task that fired on its own would spend model calls, and
            # against a real endpoint behind HEPH_DEV_CHAT_* it would spend money — on a
            # schedule nobody set, in a workspace nobody is watching.
            "enabled": False,
            "schedule": {"type": "cron", "cron": "0 8 * * *"},
            "output": "notification",
        },
    )
    if created.status_code not in (200, 201):
        return f"could not create ({created.status_code})"
    return "1 task (disabled — it must not fire unattended)"
