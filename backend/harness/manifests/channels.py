"""Messaging channels: the shared hub a channel plugin (Discord, …) plugs into.

The hub is core so every channel shares one allowlist, one link table and one relay; the
platforms themselves are built-in plugins contributing ``channels``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

from fastapi import FastAPI, HTTPException

from agent.parking import ParkedTurn
from core.exceptions import HephaestusError
from harness.manifest import FeatureManifest, FeatureRuntime, HarnessContext
from routes import channels as channels_routes
from routes.chat.operator_send import answer_approval, send_operator_message
from routes.chat.schemas import ChatCreate, ChatQueued
from routes.deps import OPERATOR_ID
from routes.runs import ApprovalDecision, ApprovalDecisions
from runs import RunRegistry, RunStatus
from services.channels import (
    ApprovalRequest,
    ApprovalStale,
    ChannelHub,
    ChannelLinks,
    ChannelRefused,
    Decision,
    PendingCall,
)
from services.channels.approvals import ChannelApprovals
from services.conversations import ConversationStore
from services.notifications import NotificationService
from services.permissions.projections import describe
from services.settings_store import SettingsStore


def _app(ctx: HarnessContext) -> FastAPI:
    if ctx.app is None:
        raise ChannelRefused("The app is not running.")
    return ctx.app


async def _build(ctx: HarnessContext) -> FeatureRuntime:
    runs = ctx.services.get(RunRegistry)

    async def send_chat(prompt: str, conversation_id: str | None) -> tuple[str, bool]:
        try:
            sent = await send_operator_message(
                _app(ctx), ChatCreate(prompt=prompt, conversation_id=conversation_id)
            )
        except HTTPException as exc:
            if exc.status_code == 404 and conversation_id is not None:
                return await send_chat(prompt, None)
            raise ChannelRefused(str(exc.detail)) from None
        except HephaestusError as exc:
            raise ChannelRefused(str(exc)) from None
        return sent.conversation_id, isinstance(sent, ChatQueued)

    async def answer(run_id: str, decisions: tuple[Decision, ...]) -> None:
        body = ApprovalDecisions(
            decisions=[
                ApprovalDecision(tool_call_id=d.tool_call_id, approved=d.approved)
                for d in decisions
            ]
        )
        try:
            _ = await answer_approval(_app(ctx), run_id, body)
        except HTTPException as exc:
            # 404, 409 and 400: the run finished, resumed, or parked again on other calls.
            if exc.status_code in (400, 404, 409):
                raise ApprovalStale(str(exc.detail)) from None
            raise

    def pending(run_id: str) -> ApprovalRequest | None:
        run = runs.get(run_id)
        if run is None or run.conversation_id is None or run.status is not RunStatus.awaiting_input:
            return None
        parked = run.parked_payload
        if not isinstance(parked, ParkedTurn):
            return None
        calls = tuple(
            _pending_call(call.tool_call_id, call.tool_name, call.args_as_dict())
            for call in parked.requests.approvals
            if call.tool_call_id not in parked.settled
        )
        if not calls:
            return None
        ids = ",".join(sorted(call.tool_call_id for call in calls))
        return ApprovalRequest(
            run_id=run.id,
            conversation_id=run.conversation_id,
            calls=calls,
            token=hashlib.sha256(ids.encode()).hexdigest()[:16],
            questions=len(parked.requests.calls),
        )

    links = ChannelLinks(ctx.engine)
    hub = ChannelHub(
        owner_id=OPERATOR_ID,
        links=links,
        settings=ctx.services.get(SettingsStore),
        conversations=ctx.services.get(ConversationStore),
        send_chat=send_chat,
        answer=answer,
        pending=pending,
    )
    ctx.services.get(NotificationService).add_channel(ChannelApprovals(hub))
    return FeatureRuntime(services=(hub, links), run_terminal=(hub.on_run_terminal,))


def _pending_call(tool_call_id: str, tool: str, args: Mapping[str, object]) -> PendingCall:
    summary, detail = describe(tool, args, fallback=f"run {tool}")
    return PendingCall(tool_call_id, tool, summary, detail)


MANIFEST = FeatureManifest(
    name="channels",
    after=("notifications",),
    routers=(channels_routes.router,),
    build=_build,
)
