"""Everything messaging channels share: the allowlist, the address → conversation links,
``/new``, relaying each turn's ending back, and settling approvals.

The hub never composes a turn or decides an approval itself. It is handed ``send_chat``
and ``answer`` (the app's own operator paths), so a message from a channel queues, claims
and composes exactly as one typed in the app, and a guarded approval is refused here as it
is for any caller without the operator's session.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from core.exceptions import PermissionDeniedError
from core.serde import parse_json
from runs import Run, RunStatus, is_chat_turn
from services.conversations import ConversationStore
from services.settings_store import SettingsStore

from .base import ApprovalRequest, Inbound, MessagingChannel
from .links import ChannelLinks

logger = logging.getLogger(__name__)

NEW_COMMAND = "/new"


class ChannelRefused(Exception):
    """The app could not take the message; the text says why, for the sender."""


@dataclass(frozen=True)
class Decision:
    tool_call_id: str
    approved: bool


#: Start a turn with ``prompt`` in ``conversation_id`` (a new chat when None). Returns the
#: conversation and whether it queued behind a running turn. Raises ``ChannelRefused``.
type SendChat = Callable[[str, str | None], Awaitable[tuple[str, bool]]]
#: Decide a parked run's pending calls. Raises ``PermissionDeniedError`` for a guarded one.
type AnswerApproval = Callable[[str, tuple[Decision, ...]], Awaitable[None]]
#: The run's undecided approvals, or None when it is not parked on any.
type PendingApprovals = Callable[[str], ApprovalRequest | None]


class ChannelHub:
    def __init__(
        self,
        *,
        owner_id: str,
        links: ChannelLinks,
        settings: SettingsStore,
        conversations: ConversationStore,
        send_chat: SendChat,
        answer: AnswerApproval,
        pending: PendingApprovals,
    ) -> None:
        self.owner_id: str = owner_id
        self.links: ChannelLinks = links
        self._settings: SettingsStore = settings
        self._conversations: ConversationStore = conversations
        self._send_chat: SendChat = send_chat
        self._answer: AnswerApproval = answer
        self._pending: PendingApprovals = pending
        self._channels: dict[str, MessagingChannel] = {}

    # --- channels -----------------------------------------------------------------

    async def attach(self, channel: MessagingChannel) -> None:
        self._channels[channel.key] = channel
        await channel.start(self)

    async def detach(self, key: str) -> None:
        channel = self._channels.pop(key, None)
        if channel is not None:
            await channel.stop()

    def channel(self, key: str) -> MessagingChannel | None:
        return self._channels.get(key)

    # --- who may write ------------------------------------------------------------

    async def allowed_senders(self, channel: str) -> list[str]:
        raw = await self._settings.get(self.owner_id, _allow_key(channel))
        try:
            stored = parse_json(raw) if raw else []
        except ValueError:
            return []
        return [s for s in stored if isinstance(s, str)] if isinstance(stored, list) else []

    async def set_allowed_senders(self, channel: str, senders: list[str]) -> None:
        clean = sorted({s.strip() for s in senders if s.strip()})
        await self._settings.set(self.owner_id, _allow_key(channel), json.dumps(clean))

    async def allowed(self, channel: str, sender: str) -> bool:
        return sender in await self.allowed_senders(channel)

    # --- messages in ------------------------------------------------------------

    async def inbound(self, channel: str, sender: str, address: str, text: str) -> Inbound:
        """A message the operator sent from ``address``: start or continue its turn."""
        if not await self.allowed(channel, sender):
            return Inbound("refused")
        if text.strip().lower() == NEW_COMMAND:
            await self.links.unlink(self.owner_id, channel, address)
            return Inbound("fresh", reply="Started fresh. Your next message opens a new chat.")
        linked = await self.links.conversation(self.owner_id, channel, address)
        try:
            conversation_id, queued = await self._send_chat(text, linked)
        except ChannelRefused as exc:
            return Inbound("failed", reply=str(exc))
        if conversation_id != linked:
            await self.links.link(self.owner_id, channel, address, conversation_id)
        return Inbound("queued" if queued else "started", conversation_id=conversation_id)

    # --- answers out ------------------------------------------------------------

    async def on_run_terminal(self, run: Run, watched: bool) -> None:
        """Send a linked conversation's turn ending to every address continuing it."""
        del watched
        if run.conversation_id is None or not is_chat_turn(run.kind):
            return
        targets = await self.links.addresses(self.owner_id, run.conversation_id)
        if not targets:
            return
        text = await self._ending(run)
        for key, address in targets:
            channel = self._channels.get(key)
            if channel is None:
                continue
            try:
                await channel.send(address, text)
            except Exception:
                logger.warning("channels: could not relay run %s to %s", run.id, key, exc_info=True)

    async def _ending(self, run: Run) -> str:
        match run.status:
            case RunStatus.done:
                answer = await self._conversations.latest_answer(run.conversation_id or "")
                return answer or "(The turn finished without a reply.)"
            case RunStatus.cancelled:
                return "The turn was stopped."
            case RunStatus.blocked:
                return "The turn stopped at a limit. Continue it in the app."
            case _:
                return f"The turn failed: {run.error or 'unknown error'}"

    # --- approvals --------------------------------------------------------------

    async def request_approvals(self, run_id: str, conversation_id: str) -> None:
        """Show a parked run's pending calls wherever its conversation is linked, or where
        a channel takes approvals for threads that aren't."""
        request = self._pending(run_id)
        if request is None:
            return
        targets = await self.links.addresses(self.owner_id, conversation_id)
        if not targets:
            for key, channel in self._channels.items():
                fallback = await channel.approval_fallback()
                if fallback is not None:
                    targets.append((key, fallback))
        for key, address in targets:
            channel = self._channels.get(key)
            if channel is not None:
                await channel.request_approval(address, request)

    async def decide(
        self, channel: str, sender: str, run_id: str, token: str, *, approve: bool
    ) -> str:
        """Approve or deny everything a run is parked on, from a channel's button. Returns
        what to tell the sender."""
        if not await self.allowed(channel, sender):
            return "You can't decide approvals from here."
        request = self._pending(run_id)
        if request is None or request.token != token:
            return "Already decided."
        if request.questions:
            return "This turn also asks you a question; answer it in the app."
        decisions = tuple(Decision(call.tool_call_id, approve) for call in request.calls)
        try:
            await self._answer(run_id, decisions)
        except PermissionDeniedError:
            return "One of these calls can only be approved in the app."
        return "Approved." if approve else "Denied."


def _allow_key(channel: str) -> str:
    return f"channels.{channel}.allowed_senders"
