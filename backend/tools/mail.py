"""Mail tools (`EMAIL-1..4`) — the agent's read/write access to the operator's mailboxes.

A thin adapter over :class:`services.mail.MailService`, with two rules that make it
different from the other toolsets:

**Every message body is untrusted.** Mail is the one ingress an attacker fully controls —
they choose the words, and those words land in the model's context. So *nothing* read from
a mailbox reaches the model unfenced: subjects, sender names, snippets, summaries and
bodies all go through :mod:`core.untrusted` (`XC-SEC-5`). The fence carries a per-call
nonce the content cannot predict, so a message cannot forge its own closing marker to
"break out" of the fence and issue instructions.

**Sending is sensitive.** ``mail_send`` and ``mail_reply`` are ``requires_approval=True``
(`AE-3.1`): they leave the machine, they cannot be undone, and they speak in the operator's
name. Each carries a plain-language ``explanation`` the operator judges the call on, exactly
like ``code_run_host_command``. Whether any of these actually pauses is the thread's
permission level to decide, so no description here claims it.
"""

from __future__ import annotations

import secrets

from pydantic_ai import FunctionToolset, ModelRetry, RunContext

from core.exceptions import NotFoundError
from core.untrusted import untrusted_fence, untrusted_preamble, wrap_untrusted
from services.mail import MailError, MailService, MessageDetail, MessageView

from .deps import RunDeps

# The capability isn't wired for this run — the model is told plainly rather than shown a
# broken tool, and adapts (the degrade posture every toolset here shares).
_UNAVAILABLE = {
    "ok": False,
    "error": "Email is not available: no mail account is connected in this workspace.",
}

# How much of a body one read returns before it is truncated. Mail is unbounded in
# principle; the context is not.
_BODY_MAX_CHARS = 12_000

# Bounds on one listing — a model asking for "all of it" still gets a page.
_LIST_MAX = 50


def _service(ctx: RunContext[RunDeps]) -> MailService | None:
    """The mail capability for this run, or ``None`` when no account is connected (or the
    service isn't wired) — in which case every tool here degrades instead of raising."""
    return ctx.deps.caps.get_optional(MailService)


def _summarize(view: MessageView) -> str:
    """One listing row, as text the model reads. Everything here came from the sender, so
    the caller fences the whole batch."""
    flags = [] if view.seen else ["unread"]
    if view.flagged:
        flags.append("flagged")
    parts = [
        f"id: {view.id}",
        f"from: {view.from_name or ''} <{view.from_address}>".strip(),
        f"subject: {view.subject}",
        f"received: {view.received_at.isoformat()}",
        f"urgency: {view.urgency}",
    ]
    if view.tags:
        parts.append(f"tags: {', '.join(view.tags)}")
    if flags:
        parts.append(f"state: {', '.join(flags)}")
    if view.summary:
        parts.append(f"summary: {view.summary}")
    parts.append(f"preview: {view.snippet}")
    return "\n".join(parts)


def _detail_text(detail: MessageDetail) -> str:
    """A read message, body included — the sender's own prose, not the history they
    quoted under it (`EMAIL-4`), which keeps a long thread from flooding the context."""
    view = detail.message
    body = detail.reply_text or detail.body
    lines = [
        f"from: {view.from_name or ''} <{view.from_address}>".strip(),
        f"to: {', '.join(view.to)}",
        f"subject: {view.subject}",
        f"received: {view.received_at.isoformat()}",
        "",
        body[:_BODY_MAX_CHARS],
    ]
    if len(body) > _BODY_MAX_CHARS:
        lines.append(f"[… body truncated at {_BODY_MAX_CHARS:,} characters]")
    if detail.signature:
        lines += ["", f"signature: {detail.signature}"]
    if detail.quoted_text:
        lines += ["", "[this message quoted earlier correspondence, omitted here]"]
    return "\n".join(lines)


def mail_toolset() -> FunctionToolset[RunDeps]:
    """The mail category (`EMAIL-1..4`)."""
    toolset: FunctionToolset[RunDeps] = FunctionToolset()

    @toolset.tool
    async def list_accounts(ctx: RunContext[RunDeps]) -> dict:
        """List the operator's connected email accounts.

        Returns each account's id, label and address. Use an account id to scope
        ``mail_list_messages`` or to send from a particular mailbox.
        """
        service = _service(ctx)
        if service is None:
            return _UNAVAILABLE
        accounts = await service.list_accounts(ctx.deps.owner_id)
        return {
            "ok": True,
            "accounts": [
                {
                    "id": account.id,
                    "name": account.name,
                    "address": account.address,
                    "enabled": account.enabled,
                    "status": account.status,
                }
                for account in accounts
            ],
        }

    @toolset.tool
    async def list_messages(
        ctx: RunContext[RunDeps],
        account_id: str | None = None,
        folder: str | None = None,
        limit: int = 20,
        unread_only: bool = False,
    ) -> dict:
        """List recent email, newest first, with its automatic triage.

        Each entry carries the message id (pass it to ``mail_read``), sender, subject,
        received time, urgency, category tags and a one-line summary. Spam is excluded.
        Omit ``account_id`` to read across every connected account.
        """
        service = _service(ctx)
        if service is None:
            return _UNAVAILABLE
        try:
            messages = await service.list_messages(
                ctx.deps.owner_id,
                account_id=account_id,
                folder=folder,
                limit=max(1, min(limit, _LIST_MAX)),
                unread_only=unread_only,
            )
        except NotFoundError as exc:
            raise ModelRetry(f"{exc}. Call mail_list_accounts for the valid ids.") from exc
        except MailError as exc:
            return {"ok": False, "error": str(exc)}
        if not messages:
            return {"ok": True, "count": 0, "messages": []}
        # One preamble for the batch, one fence per message sharing its nonce — the shape
        # `search` already uses, so a listing doesn't repeat the warning on every row.
        nonce = secrets.token_hex(8)
        return {
            "ok": True,
            "count": len(messages),
            "instruction": untrusted_preamble(nonce),
            "messages": [
                untrusted_fence(_summarize(view), nonce, source="email") for view in messages
            ],
        }

    @toolset.tool
    async def read(ctx: RunContext[RunDeps], message_id: str) -> dict:
        """Read one email in full by its id, fetching the body if it isn't cached yet.

        Returns the sender's own text, with quoted history and signature separated out.
        """
        service = _service(ctx)
        if service is None:
            return _UNAVAILABLE
        try:
            detail = await service.read_message(ctx.deps.owner_id, message_id)
        except NotFoundError as exc:
            raise ModelRetry(f"{exc}. Use mail_list_messages to get a valid message id.") from exc
        except MailError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "message": wrap_untrusted(_detail_text(detail), source="email")}

    @toolset.tool
    async def draft_reply(ctx: RunContext[RunDeps], message_id: str, body: str) -> dict:
        """Save a reply draft for the operator to review, **without sending it**.

        Prefer this over ``mail_reply`` whenever the operator has not clearly asked for
        the message to go out: nothing leaves the machine, and they can edit it first.
        """
        service = _service(ctx)
        if service is None:
            return _UNAVAILABLE
        try:
            detail = await service.cache.get(ctx.deps.owner_id, message_id)
            draft = await service.drafts.create(
                ctx.deps.owner_id,
                detail.message.account_id,
                in_reply_to_id=message_id,
                to=[detail.message.from_address],
                subject=detail.message.subject,
                body=body,
            )
        except NotFoundError as exc:
            raise ModelRetry(str(exc)) from exc
        except MailError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "draft_id": draft.id, "saved": True, "sent": False}

    @toolset.tool(requires_approval=True)
    async def send(
        ctx: RunContext[RunDeps],
        account_id: str,
        to: list[str],
        subject: str,
        body: str,
        explanation: str,
        cc: list[str] | None = None,
    ) -> dict:
        """Send a new email from one of the operator's accounts.

        This leaves the machine in the operator's name and cannot be undone.
        ``explanation`` MUST say who this goes to and what it says.
        """
        service = _service(ctx)
        if service is None:
            return _UNAVAILABLE
        try:
            message_id = await service.send(
                ctx.deps.owner_id, account_id, to=to, subject=subject, body=body, cc=cc
            )
        except NotFoundError as exc:
            raise ModelRetry(f"{exc}. Call mail_list_accounts for the valid ids.") from exc
        except MailError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "sent": True, "message_id": message_id}

    @toolset.tool(requires_approval=True)
    async def reply(
        ctx: RunContext[RunDeps],
        message_id: str,
        body: str,
        explanation: str,
        reply_all: bool = False,
    ) -> dict:
        """Reply to an email, threading the response correctly.

        Sending is irreversible and speaks in the operator's name. ``explanation`` MUST
        plainly describe what the reply says and who receives it. To prepare a response
        *without* sending it, use ``mail_draft_reply`` instead.
        """
        service = _service(ctx)
        if service is None:
            return _UNAVAILABLE
        try:
            sent_id = await service.reply(ctx.deps.owner_id, message_id, body, reply_all=reply_all)
        except NotFoundError as exc:
            raise ModelRetry(str(exc)) from exc
        except MailError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "sent": True, "message_id": sent_id}

    @toolset.tool
    async def mark(
        ctx: RunContext[RunDeps],
        message_id: str,
        seen: bool | None = None,
        flagged: bool | None = None,
    ) -> dict:
        """Mark an email read/unread or flagged/unflagged."""
        service = _service(ctx)
        if service is None:
            return _UNAVAILABLE
        try:
            await service.set_flags(ctx.deps.owner_id, message_id, seen=seen, flagged=flagged)
        except NotFoundError as exc:
            raise ModelRetry(str(exc)) from exc
        except MailError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True}

    return toolset
