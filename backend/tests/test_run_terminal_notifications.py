"""End-to-end wiring: app.py's `on_terminal` composition over NotificationService —
run_failed always, run_completed only when unwatched, cancelled/blocked never notify,
and a stateless (no-conversation) run never notifies either."""

from __future__ import annotations

import asyncio

from routes.deps import OPERATOR_ID
from runs.events import AnswerDelta

from ._helpers import client_app


async def _drain_terminal_notify_tasks(app):
    """Await every in-flight run-terminal notify task. `_on_run_terminal` adds its
    task to `run_terminal_tasks` synchronously, before the registry's own `run.wait()`
    can return — so by the time a caller reaches here the task is already registered
    (just maybe not yet run to completion); one `gather` is enough to settle it."""
    pending = list(app.state.run_terminal_tasks)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


async def _run_notifications(app, run_id):
    await _drain_terminal_notify_tasks(app)
    items, _ = await app.state.notifications.list_notifications(OPERATOR_ID, limit=100)
    return [n for n in items if n.run_id == run_id]


async def test_run_failed_always_notifies():
    async with client_app() as (client, app):
        conv_id = await app.state.conversations.create_conversation(OPERATOR_ID)

        async def orch(run):
            raise ValueError("boom")

        run = app.state.runs.submit(
            kind="chat", owner_id=OPERATOR_ID, orchestrator=orch, conversation_id=conv_id
        )
        await run.wait()

        notifs = await _run_notifications(app, run.id)
        assert len(notifs) == 1
        assert notifs[0].kind == "run_failed"
        assert notifs[0].conversation_id == conv_id
        assert notifs[0].body == "boom"


async def test_run_completed_notifies_when_nobody_is_watching():
    async with client_app() as (client, app):
        conv_id = await app.state.conversations.create_conversation(OPERATOR_ID)

        async def orch(run):
            run.emit(AnswerDelta(text="hi"))

        run = app.state.runs.submit(
            kind="chat", owner_id=OPERATOR_ID, orchestrator=orch, conversation_id=conv_id
        )
        await run.wait()

        notifs = await _run_notifications(app, run.id)
        assert len(notifs) == 1
        assert notifs[0].kind == "run_completed"


async def test_run_completed_is_suppressed_with_a_live_subscriber():
    async with client_app() as (client, app):
        conv_id = await app.state.conversations.create_conversation(OPERATOR_ID)
        started, release = asyncio.Event(), asyncio.Event()

        async def orch(run):
            run.emit(AnswerDelta(text="hi"))
            started.set()
            await release.wait()

        run = app.state.runs.submit(
            kind="chat", owner_id=OPERATOR_ID, orchestrator=orch, conversation_id=conv_id
        )
        await started.wait()

        async def consume():
            async for _ in run.stream.subscribe():
                pass

        task = asyncio.create_task(consume())
        await asyncio.sleep(0)  # let it register before the run finishes
        release.set()
        await run.wait()
        await task

        assert await _run_notifications(app, run.id) == []


async def test_cancelled_run_never_notifies():
    async with client_app() as (client, app):
        conv_id = await app.state.conversations.create_conversation(OPERATOR_ID)

        async def orch(run):
            await asyncio.Event().wait()  # never completes on its own

        run = app.state.runs.submit(
            kind="chat", owner_id=OPERATOR_ID, orchestrator=orch, conversation_id=conv_id
        )
        await asyncio.sleep(0)
        assert await app.state.runs.cancel(run.id) is True
        await run.wait()

        assert await _run_notifications(app, run.id) == []


async def test_blocked_run_never_notifies():
    async with client_app() as (client, app):
        conv_id = await app.state.conversations.create_conversation(OPERATOR_ID)

        async def orch(run):
            run.block("need more info")

        run = app.state.runs.submit(
            kind="agent", owner_id=OPERATOR_ID, orchestrator=orch, conversation_id=conv_id
        )
        await run.wait()

        assert await _run_notifications(app, run.id) == []


async def test_a_run_with_no_conversation_never_notifies():
    async with client_app() as (client, app):

        async def orch(run):
            raise ValueError("boom")

        run = app.state.runs.submit(kind="chat", owner_id=OPERATOR_ID, orchestrator=orch)
        await run.wait()

        assert await _run_notifications(app, run.id) == []


async def test_a_hidden_threads_run_never_notifies():
    """A notification is a deep link, and a sub-agent's thread is deliberately kept out of
    the session list — so announcing one sends the operator somewhere they cannot get back
    from, and a fan-out of six researchers announces six of them at once. What they watch
    is the card; the thread that launched them notifies for itself."""
    async with client_app() as (client, app):
        conv_id = await app.state.conversations.create_conversation(OPERATOR_ID, ephemeral=True)

        async def orch(run):
            run.emit(AnswerDelta(text="what I found"))

        run = app.state.runs.submit(
            kind="linked", owner_id=OPERATOR_ID, orchestrator=orch, conversation_id=conv_id
        )
        await run.wait()

        assert await _run_notifications(app, run.id) == []


async def test_a_hidden_threads_failure_is_silent_too():
    """The same, for the branch that notifies even when somebody *is* watching. A
    sub-agent that fails is shown on its card in an error tone and its report says so —
    a toast pointing at a thread they cannot open adds nothing they can act on."""
    async with client_app() as (client, app):
        conv_id = await app.state.conversations.create_conversation(OPERATOR_ID, ephemeral=True)

        async def orch(run):
            raise ValueError("boom")

        run = app.state.runs.submit(
            kind="linked", owner_id=OPERATOR_ID, orchestrator=orch, conversation_id=conv_id
        )
        await run.wait()

        assert await _run_notifications(app, run.id) == []


async def test_a_raising_notifier_never_affects_the_runs_own_outcome(monkeypatch):
    async with client_app() as (client, app):
        conv_id = await app.state.conversations.create_conversation(OPERATOR_ID)

        async def boom(*args, **kwargs):
            raise RuntimeError("notifier exploded")

        monkeypatch.setattr(app.state.notifications, "notify", boom)

        async def orch(run):
            run.emit(AnswerDelta(text="hi"))

        run = app.state.runs.submit(
            kind="chat", owner_id=OPERATOR_ID, orchestrator=orch, conversation_id=conv_id
        )
        await run.wait()
        await _drain_terminal_notify_tasks(app)

        assert run.status.value == "done"  # the run's own outcome is untouched
