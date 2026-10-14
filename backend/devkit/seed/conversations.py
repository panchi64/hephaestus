"""Threads, made the way threads are actually made.

There is no route that inserts a conversation — a conversation is what happens when
someone sends a message, so this sends messages and waits for the runs to finish. That is
slower than writing rows would be, and it is the only honest option: the transcript
shape, the branch tree, the title, the token accounting and the message ordering are all
produced by the engine, and a hand-built row would differ from a real one in exactly the
ways a rendering bug hides in.

It doubles as the instance's smoke test. If seeding produces threads, the whole path —
registry, engine, provider adapter, run substrate, event stream, persistence — worked.
"""

from __future__ import annotations

import asyncio
import json
from typing import TypedDict

import httpx

from devkit import live
from devkit.seed.registry import SeedContext, fixture, rows

#: How long one seeded turn may take before seeding gives up on it. Generous for a real
#: endpoint behind `HEPH_DEV_CHAT_*`, and far beyond anything the stub needs.
_TURN_TIMEOUT_S = 120.0

#: Pushing a script is a local POST that either answers at once or is broken.
_SCRIPT_TIMEOUT_S = 10.0


class _Thread(TypedDict):
    prompt: str
    script: list[dict[str, object]]


#: The scripted exchanges. The second is the one worth having: an assistant turn that
#: calls a tool and then answers from its result is the shape most of the chat UI exists
#: to render, and an instance seeded only with plain replies leaves all of that untested.
_THREADS: list[_Thread] = [
    {
        "prompt": "Give me a one-line summary of what this workspace is for.",
        "script": [
            # `needs_tools` is what tells the auto-titler's request apart from the turn's:
            # both carry this same last user message, and only the turn offers tools.
            # Without the distinction the thread is named after the whole answer.
            {"match": "one-line summary", "needs_tools": False, "text": "What this workspace is"},
            {
                "match": "one-line summary",
                "needs_tools": True,
                "text": (
                    "This is a development instance — its own data, its own ports, and a "
                    "scripted model behind it, so nothing here touches your real workspace."
                ),
            },
        ],
    },
    {
        "prompt": "What time is it, and what should I know about today?",
        "script": [
            {"match": "what time is it", "needs_tools": False, "text": "Today at a glance"},
            {
                "match": "what time is it",
                "tool_calls": [{"name": "builtin_now", "arguments": "{}"}],
                "uses": 1,
            },
            {
                "match": "what time is it",
                "needs_tools": True,
                "text": (
                    "I checked the clock. Nothing is scheduled that needs you right now — "
                    "this is a seeded thread, so the calendar it read is the fixture one."
                ),
            },
        ],
    },
]


async def _push_script(ctx: SeedContext, script: list[dict[str, object]]) -> None:
    """Tell the stub what to say for this thread. A no-op against a real endpoint —
    there is nothing to script, and the reply will simply be whatever it says."""
    if live.configured():
        return
    # Its own client: the seed context's is pointed at the backend, and the stub is a
    # different server on a different port. Bounded, so a stub that accepts the
    # connection and then never answers fails this fixture rather than the whole run.
    base_url = f"http://127.0.0.1:{ctx.instance.stub_port}"
    async with httpx.AsyncClient(base_url=base_url, timeout=_SCRIPT_TIMEOUT_S) as stub:
        await stub.post("/_stub/script", json=script)


async def _drain(ctx: SeedContext, run_id: str) -> bool:
    """Follow a run's event stream to its end. ``False`` if it did not finish in time.

    Draining rather than polling because the stream is how the app itself learns a run
    ended, and a seed that watched some other signal could report success for a turn the
    UI would still show as running.
    """
    try:
        async with asyncio.timeout(_TURN_TIMEOUT_S):
            async with ctx.client.stream("GET", f"/runs/{run_id}/events") as response:
                if response.status_code != 200:
                    return False
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    event = json.loads(line[len("data:") :].strip())
                    # `run.ended` and `run.error` are the same terminal position in the
                    # protocol, not a frame plus an extra — so either ends the stream,
                    # and only the first means the turn actually produced an answer.
                    if event.get("type") == "run.error":
                        return False
                    if event.get("type") == "run.ended":
                        return event.get("outcome") == "done"
    except TimeoutError, json.JSONDecodeError:
        return False
    return False


@fixture("conversations", order=10)
async def seed_conversations(ctx: SeedContext) -> str:
    existing = rows(await ctx.client.get("/conversations"))
    if existing:
        return f"{len(existing)} already here — left alone"

    made = 0
    for thread in _THREADS:
        await _push_script(ctx, thread["script"])
        started = await ctx.client.post("/chat", json={"prompt": thread["prompt"]})
        if started.status_code not in (200, 202):
            continue
        if await _drain(ctx, started.json()["run_id"]):
            made += 1
    return f"{made} of {len(_THREADS)} threads" if made else "none — the engine did not answer"
