"""Mail tools — the agent's read/write access to the operator's mailboxes.

A thin adapter over :class:`plugins.mail.services.MailService`, with two rules that make it
different from the other toolsets:

**Every message body is untrusted.** Mail is the one ingress an attacker fully controls —
they choose the words, and those words land in the model's context. So *nothing* read from
a mailbox reaches the model unfenced: subjects, sender names, snippets, summaries and
bodies all go through :mod:`core.untrusted` (`XC-SEC-5`). The fence carries a per-call
nonce the content cannot predict, so a message cannot forge its own closing marker to
"break out" of the fence and issue instructions.

**Sending is sensitive.** ``mail_send`` and ``mail_reply`` are
``requires_approval=True``: they leave the machine, they cannot be undone, and they speak
in the operator's name. Each carries a plain-language ``explanation`` the operator judges
the call on, exactly like ``shell_run_on_host``. Whether any of these actually pauses is
the thread's permission level to decide, so no description here claims it.
"""

from __future__ import annotations

import secrets

from pydantic_ai import FunctionToolset, ModelRetry, RunContext

from core.diagnostics import render_tool_error
from core.exceptions import NotFoundError
from core.untrusted import untrusted_fence, untrusted_preamble, wrap_untrusted
from tools.deps import RunDeps

from .services import MailError, MailService, MessageDetail, MessageView

# The capability isn't wired for this run — the model is told plainly rather than shown a
# broken tool, and adapts (the degrade posture every toolset here shares).
_UNAVAILABLE: dict[str, object] = {
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


def _no_such_message(exc: NotFoundError) -> ModelRetry:
    return ModelRetry(
        render_tool_error(str(exc), hints=["take a message `id` from `mail_list_messages`"])
    )


def _no_such_account(exc: NotFoundError) -> ModelRetry:
    return ModelRetry(
        render_tool_error(str(exc), hints=["take an account `id` from `mail_list_accounts`"])
    )


def mail_toolset() -> FunctionToolset[RunDeps]:
    """The mail category."""
    toolset: FunctionToolset[RunDeps] = FunctionToolset()

    @toolset.tool
    async def list_accounts(ctx: RunContext[RunDeps]) -> dict[str, object]:
        """Returns the operator's connected email accounts, each with its `id`, `name`,
        `address`, whether it is `enabled`, and its sync `status`.

        Call it for the `account_id` that `mail_send` needs, or to confine
        `mail_list_messages` to one mailbox. When the operator has several accounts and
        has not said which to send from, ask rather than pick one.
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
    ) -> dict[str, object]:
        """Returns recent email, newest first, with the platform's own triage of each
        message: urgency, category tags and a one-line summary. Spam is left out.

        Use it to answer what has come in, what needs attention, or to find a message to
        open. Each entry carries the message `id` that `mail_read`, `mail_reply` and
        `mail_mark` take, the sender, subject, received time and a short preview. The
        summary and preview are excerpts, so open a message with `mail_read` before you
        quote it or act on what it asks.

        Args:
            account_id: One account to list, from `mail_list_accounts`. Omit for every
                connected account.
            folder: One folder to list, such as `INBOX`. Omit for every folder.
            limit: How many messages to return, up to 50. Default 20.
            unread_only: True to list only unread messages.
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
            raise _no_such_account(exc) from exc
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
    async def read(ctx: RunContext[RunDeps], message_id: str) -> dict[str, object]:
        """Returns one email in full: sender, recipients, subject, received time and the
        body, with its signature separated out.

        The body is the sender's own text. Earlier correspondence quoted beneath it is
        left out, and a note says so, so a long thread does not flood your context. A
        body over 12,000 characters is cut short with a note.

        Args:
            message_id: The `id` from `mail_list_messages`.
        """
        service = _service(ctx)
        if service is None:
            return _UNAVAILABLE
        try:
            detail = await service.read_message(ctx.deps.owner_id, message_id)
        except NotFoundError as exc:
            raise _no_such_message(exc) from exc
        except MailError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "message": wrap_untrusted(_detail_text(detail), source="email")}

    @toolset.tool
    async def draft_reply(
        ctx: RunContext[RunDeps], message_id: str, body: str
    ) -> dict[str, object]:
        """Saves a reply to an email as a draft for the operator to review and send
        themselves. Nothing is sent.

        Use this rather than `mail_reply` whenever the operator has not clearly asked for
        the message to go out: they can edit the draft first, and nothing leaves the
        machine on a guess. Write the body in the operator's own voice and style, since
        it goes out under their name. It replies to the sender only.

        Returns the `draft_id`.

        Args:
            message_id: The `id` of the email being answered.
            body: The reply's text, without the quoted original.
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
            raise _no_such_message(exc) from exc
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
    ) -> dict[str, object]:
        """Sends a new email from one of the operator's accounts. It goes out in their
        name and cannot be recalled.

        Use it only when the operator has asked for this message to be sent. To answer an
        existing email, use `mail_reply`, which keeps the thread together; to prepare a
        message for them to send, use `mail_draft_reply`. Write in the operator's own
        voice and style, and check every address against what they told you or what a
        message shows, because a guessed address sends their words to a stranger.

        Returns the sent `message_id`.

        Args:
            account_id: The account to send from, from `mail_list_accounts`.
            body: The message, in plain text.
            explanation: Who receives this and the gist of what it says.
        """
        del explanation  # Read by the approval card from the call's arguments, not by this body.
        service = _service(ctx)
        if service is None:
            return _UNAVAILABLE
        try:
            message_id = await service.send(
                ctx.deps.owner_id, account_id, to=to, subject=subject, body=body, cc=cc
            )
        except NotFoundError as exc:
            raise _no_such_account(exc) from exc
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
    ) -> dict[str, object]:
        """Sends a reply to an email, threaded with the original. It goes out in the
        operator's name and cannot be recalled.

        Use it only when the operator has asked for the reply to be sent. Otherwise
        prepare it with `mail_draft_reply`, which they can edit and send themselves.
        Write in the operator's own voice and style.

        Returns the sent `message_id`.

        Args:
            message_id: The `id` of the email being answered.
            body: The reply's text, without the quoted original.
            explanation: Who receives this reply and the gist of what it says.
            reply_all: True to include everyone on the original, not just its sender.
                Leave it off unless the operator wants the others included.
        """
        del explanation  # Read by the approval card from the call's arguments, not by this body.
        service = _service(ctx)
        if service is None:
            return _UNAVAILABLE
        try:
            sent_id = await service.reply(ctx.deps.owner_id, message_id, body, reply_all=reply_all)
        except NotFoundError as exc:
            raise _no_such_message(exc) from exc
        except MailError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "sent": True, "message_id": sent_id}

    @toolset.tool
    async def mark(
        ctx: RunContext[RunDeps],
        message_id: str,
        seen: bool | None = None,
        flagged: bool | None = None,
    ) -> dict[str, object]:
        """Marks an email read or unread, flagged or unflagged. Pass only the flags you
        want to change.

        Reading a message with `mail_read` does not mark it read, so the operator's
        unread count stays theirs unless they ask you to change it.

        Args:
            message_id: The `id` from `mail_list_messages`.
            seen: True for read, False for unread.
            flagged: True to flag, False to clear the flag.
        """
        service = _service(ctx)
        if service is None:
            return _UNAVAILABLE
        try:
            await service.set_flags(ctx.deps.owner_id, message_id, seen=seen, flagged=flagged)
        except NotFoundError as exc:
            raise _no_such_message(exc) from exc
        except MailError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True}

    return toolset
