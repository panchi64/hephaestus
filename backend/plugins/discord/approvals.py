"""How a parked run's approval looks on Discord, and reading a button press back."""

from __future__ import annotations

from dataclasses import dataclass

import discord

from services.channels import ApprovalRequest, PendingCall

APPROVE = "approve"
DENY = "deny"
_PREFIX = "heph"
_MAX_DETAIL = 600
_MAX_CONTENT = 2000


@dataclass(frozen=True)
class ButtonPress:
    approve: bool
    run_id: str
    token: str


def custom_id(action: str, run_id: str, token: str) -> str:
    return f"{_PREFIX}:{action}:{run_id}:{token}"


def parse_custom_id(raw: str) -> ButtonPress | None:
    """The decision a button's ``custom_id`` carries, or ``None`` for any other component."""
    parts = raw.split(":")
    if len(parts) != 4 or parts[0] != _PREFIX or parts[1] not in (APPROVE, DENY):
        return None
    if not parts[2] or not parts[3]:
        return None
    return ButtonPress(approve=parts[1] == APPROVE, run_id=parts[2], token=parts[3])


def approval_text(request: ApprovalRequest) -> str:
    """Each call whole, as many as fit in one message; the rest are counted, never cut
    mid-fence."""
    head = "**The agent is waiting for approval:**"
    tail = (
        ["It also asked a question, which has to be answered in the app."]
        if request.questions
        else []
    )
    blocks = [_call_block(call) for call in request.calls]
    shown: list[str] = []
    for index, block in enumerate(blocks):
        left = len(blocks) - index - 1
        more = [f"…and {left} more; see them in the app."] if left else []
        if len("\n".join([head, *shown, block, *more, *tail])) > _MAX_CONTENT:
            shown.append(f"…and {len(blocks) - index} more; see them in the app.")
            break
        shown.append(block)
    return "\n".join([head, *shown, *tail])


def _call_block(call: PendingCall) -> str:
    if not call.detail:
        return f"- {call.summary}"
    detail = call.detail.replace("```", "'''")
    if len(detail) > _MAX_DETAIL:
        detail = detail[:_MAX_DETAIL] + "…"
    return f"- {call.summary}\n```\n{detail}\n```"


def approval_view(request: ApprovalRequest) -> discord.ui.View:
    """Buttons that survive a restart: the press is handled from ``on_interaction``, so
    the view is never registered and its timeout is off."""
    view = discord.ui.View(timeout=None)
    _ = view.add_item(
        discord.ui.Button(
            style=discord.ButtonStyle.success,
            label="Approve all",
            custom_id=custom_id(APPROVE, request.run_id, request.token),
        )
    )
    _ = view.add_item(
        discord.ui.Button(
            style=discord.ButtonStyle.danger,
            label="Deny all",
            custom_id=custom_id(DENY, request.run_id, request.token),
        )
    )
    return view
