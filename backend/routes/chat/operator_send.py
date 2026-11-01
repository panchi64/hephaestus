"""The operator acting from somewhere other than the app: a messaging channel, a plugin window.

Each call runs the very route the app's own composer and approval card use, so a message
from Discord queues, claims and composes exactly as one typed in the app does. The request
carries no session, so anything gated on the operator's own session (``require_operator``)
refuses it, as it would refuse an API token.
"""

from __future__ import annotations

from fastapi import FastAPI, Request

from routes.chat.schemas import ChatCreate, ChatCreated, ChatQueued
from routes.chat.send import create_chat
from routes.runs import ApprovalDecisions, ApprovalOutcome, approve_run


def operator_request(app: FastAPI, path: str) -> Request:
    """A sessionless request against ``app``, standing in for the operator's own."""
    return Request(
        {
            "type": "http",
            "app": app,
            "method": "POST",
            "scheme": "http",
            "server": ("operator", 0),
            "path": path,
            "root_path": "",
            "query_string": b"",
            "headers": [],
        }
    )


async def send_operator_message(app: FastAPI, body: ChatCreate) -> ChatCreated | ChatQueued:
    """Start a turn, or queue one behind the thread's running turn."""
    return await create_chat(body, operator_request(app, "/chat"))


async def answer_approval(
    app: FastAPI, run_id: str, decisions: ApprovalDecisions
) -> ApprovalOutcome:
    """Decide a parked run's pending calls. A guarded call is refused: only the operator's
    own session may answer one."""
    return await approve_run(run_id, decisions, operator_request(app, f"/runs/{run_id}/approve"))
