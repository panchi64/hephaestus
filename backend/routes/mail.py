"""Mail surface (`EMAIL-1..5`) — the Email screen's REST contract.

Thin pass-throughs to :class:`~services.mail.service.MailService`; every decision the
screen appears to make (what's urgent, what's spam, which folder is Sent, whether a
provider supports a move) was already made below this layer. Out-shapes are camelCase to
match the frontend's ``email`` seam, and message bodies come back decrypted — the operator
owns their own mail.

Two things are deliberately *not* here:

- **No approval gate on sending.** An operator pressing SEND in their own client has
  already consented; the gate belongs on the *agent's* tool (``tools/mail.py``, `AE-3.1`),
  which is a different actor asking for a different thing.
- **No untrusted fencing.** Fencing is for content on its way into a *model's* context
  (`XC-SEC-5`). This content is on its way to a human reading their own inbox, where a
  fence would be noise.

Domain errors map here rather than leaking: a missing account/message is 404, a rejected
credential is 401, an unreachable provider is 503 (the caller retries or reads the cache),
an unsupported operation is 409, and anything else the operator can act on is 400.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from core.exceptions import NotFoundError
from routes import deps
from routes.camel import CamelModel
from routes.deps import OPERATOR_ID
from services.mail import (
    AccountView,
    DraftView,
    MailAuthError,
    MailError,
    MailFolder,
    MailUnavailableError,
    MailUnsupportedError,
    MessageDetail,
    MessageView,
    StyleProfileView,
)

router = APIRouter(prefix="/mail", tags=["mail"])


class AccountOut(CamelModel):
    id: str
    name: str
    address: str
    provider: str
    auth_kind: str
    enabled: bool
    # Last known reachability, recorded by a probe or a sync (`XC-DEG-3`).
    status: str
    error_detail: str | None = None
    last_synced_at: datetime | None = None


class FolderOut(CamelModel):
    id: str
    account_id: str
    name: str
    # Normalized across providers ("inbox" | "sent" | …) so the screen can find the Sent
    # folder without telling IMAP special-use flags apart from Gmail label ids.
    role: str
    count: int


class MessageOut(CamelModel):
    """A message as the inbox renders it. ``body`` is empty on a listing and filled on a
    read — one shape either way, so the list and the reading pane share a type."""

    id: str
    account_id: str
    folder_id: str
    # `from` is a Python keyword, so the attribute is `from_` and the wire key is set
    # explicitly (the camel generator would otherwise emit `from_`).
    from_address: str = Field(serialization_alias="from")
    from_name: str
    to: list[str]
    subject: str
    snippet: str
    body: str
    received_at: datetime
    read: bool
    flagged: bool
    urgency: str
    tags: list[str]
    spam: bool
    summary: str
    # `EMAIL-4`: the sender's own prose, split from what they quoted and their sign-off, so
    # a reading pane can show the reply without the whole thread under it.
    reply_text: str = ""
    quoted_text: str | None = None
    signature: str | None = None


class ReplySuggestionOut(CamelModel):
    id: str
    label: str
    body: str


class StyleProfileOut(CamelModel):
    """`EMAIL-3` — the learned writing profile, which the operator may rewrite. ``edited``
    records that they did, which is why a later re-learn leaves it alone."""

    profile: str
    sample_count: int
    edited: bool


class SendResult(CamelModel):
    message_id: str


class AccountCreate(BaseModel):
    name: str = ""
    address: str
    provider: str = "imap"
    config: dict | None = None
    password: str | None = None


class AccountUpdate(BaseModel):
    name: str | None = None
    config: dict | None = None
    password: str | None = None
    enabled: bool | None = None


class MessageUpdate(BaseModel):
    read: bool | None = None
    flagged: bool | None = None


class SendRequest(BaseModel):
    account_id: str
    to: list[str]
    subject: str = ""
    body: str = ""
    cc: list[str] | None = None
    bcc: list[str] | None = None


class ReplyRequest(BaseModel):
    body: str
    reply_all: bool = False


class StyleProfileUpdate(BaseModel):
    profile: str


def _account_out(view: AccountView) -> AccountOut:
    return AccountOut(
        id=view.id,
        name=view.name,
        address=view.address,
        provider=view.provider,
        auth_kind=view.auth_kind,
        enabled=view.enabled,
        status=view.status,
        error_detail=view.error_detail,
        last_synced_at=view.last_synced_at,
    )


def _folder_out(account_id: str, folder: MailFolder) -> FolderOut:
    return FolderOut(
        id=folder.id,
        account_id=account_id,
        name=folder.name,
        role=folder.role,
        # Unread is what an inbox badge means; total stands in when a provider reports
        # only the one.
        count=folder.unread if folder.unread is not None else (folder.total or 0),
    )


def _message_out(view: MessageView, detail: MessageDetail | None = None) -> MessageOut:
    return MessageOut(
        id=view.id,
        account_id=view.account_id,
        folder_id=view.folder,
        from_address=view.from_address,
        from_name=view.from_name or view.from_address,
        to=list(view.to),
        subject=view.subject,
        snippet=view.snippet,
        body=detail.body if detail is not None else "",
        received_at=view.received_at,
        read=view.seen,
        flagged=view.flagged,
        urgency=view.urgency,
        tags=list(view.tags),
        spam=view.spam,
        summary=view.summary or "",
        reply_text=detail.reply_text if detail is not None else "",
        quoted_text=detail.quoted_text if detail is not None else None,
        signature=detail.signature if detail is not None else None,
    )


def _suggestion_out(draft: DraftView) -> ReplySuggestionOut:
    return ReplySuggestionOut(id=draft.id, label=draft.label or "SUGGESTED REPLY", body=draft.body)


def _profile_out(stored: StyleProfileView | None) -> StyleProfileOut | None:
    if stored is None or stored.profile is None:
        return None
    return StyleProfileOut(
        profile=stored.profile, sample_count=stored.sample_count, edited=stored.edited
    )


def _fail(exc: MailError) -> HTTPException:
    """The one place the domain's error taxonomy becomes status codes, so every endpoint
    reports the same failure the same way."""
    if isinstance(exc, MailAuthError):
        return HTTPException(status_code=401, detail=str(exc))
    if isinstance(exc, MailUnavailableError):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, MailUnsupportedError):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


# --- accounts ----------------------------------------------------------------


@router.get("/accounts", response_model=list[AccountOut])
async def list_accounts(request: Request) -> list[AccountOut]:
    return [_account_out(v) for v in await deps.mail(request).list_accounts(OPERATOR_ID)]


@router.post("/accounts", status_code=201, response_model=AccountOut)
async def create_account(body: AccountCreate, request: Request) -> AccountOut:
    if not body.address.strip():
        raise HTTPException(status_code=422, detail="an account needs an address")
    try:
        view = await deps.mail(request).create_account(
            OPERATOR_ID,
            name=body.name or body.address,
            address=body.address,
            provider=body.provider,
            config=body.config,
            password=body.password,
        )
    except MailError as exc:
        raise _fail(exc) from exc
    return _account_out(view)


@router.patch("/accounts/{account_id}", response_model=AccountOut)
async def update_account(account_id: str, body: AccountUpdate, request: Request) -> AccountOut:
    try:
        view = await deps.mail(request).update_account(
            OPERATOR_ID,
            account_id,
            name=body.name,
            config=body.config,
            password=body.password,
            enabled=body.enabled,
        )
    except NotFoundError:
        raise HTTPException(status_code=404, detail="account not found") from None
    except MailError as exc:
        raise _fail(exc) from exc
    return _account_out(view)


@router.delete("/accounts/{account_id}", status_code=204)
async def delete_account(account_id: str, request: Request) -> None:
    try:
        await deps.mail(request).delete_account(OPERATOR_ID, account_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="account not found") from None


@router.post("/accounts/{account_id}/probe", response_model=AccountOut)
async def probe_account(account_id: str, request: Request) -> AccountOut:
    """Test the connection. A *connection* failure is a recorded status, not an error —
    the returned account carries the verdict the operator reads (`XC-DEG-3`)."""
    try:
        view = await deps.mail(request).probe_account(OPERATOR_ID, account_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="account not found") from None
    return _account_out(view)


@router.get("/accounts/{account_id}/folders", response_model=list[FolderOut])
async def list_folders(account_id: str, request: Request) -> list[FolderOut]:
    try:
        folders = await deps.mail(request).list_folders(OPERATOR_ID, account_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="account not found") from None
    except MailError as exc:
        raise _fail(exc) from exc
    return [_folder_out(account_id, folder) for folder in folders]


# --- messages ----------------------------------------------------------------


@router.get("/messages", response_model=list[MessageOut])
async def list_messages(
    request: Request,
    account_id: str | None = None,
    folder: str | None = None,
    limit: int = 50,
    unread_only: bool = False,
    include_spam: bool = False,
    refresh: bool = False,
) -> list[MessageOut]:
    """The inbox listing, newest first, served from the cache (`EMAIL-5`) and refreshed
    from the provider when the freshness window has lapsed or ``refresh`` forces it."""
    try:
        views = await deps.mail(request).list_messages(
            OPERATOR_ID,
            account_id=account_id,
            folder=folder,
            limit=max(1, min(limit, 200)),
            unread_only=unread_only,
            include_spam=include_spam,
            refresh=refresh,
        )
    except NotFoundError:
        raise HTTPException(status_code=404, detail="account not found") from None
    except MailError as exc:
        raise _fail(exc) from exc
    return [_message_out(v) for v in views]


@router.get("/messages/{message_id}", response_model=MessageOut)
async def read_message(message_id: str, request: Request) -> MessageOut:
    try:
        detail = await deps.mail(request).read_message(OPERATOR_ID, message_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="message not found") from None
    except MailError as exc:
        raise _fail(exc) from exc
    return _message_out(detail.message, detail)


@router.patch("/messages/{message_id}", status_code=204)
async def update_message(message_id: str, body: MessageUpdate, request: Request) -> None:
    """Mark read/unread or flagged. The remote write is authoritative — a provider that
    refuses it leaves the local state honest rather than showing a lie."""
    try:
        await deps.mail(request).set_flags(
            OPERATOR_ID, message_id, seen=body.read, flagged=body.flagged
        )
    except NotFoundError:
        raise HTTPException(status_code=404, detail="message not found") from None
    except MailError as exc:
        raise _fail(exc) from exc


@router.delete("/messages/{message_id}", status_code=204)
async def delete_message(message_id: str, request: Request) -> None:
    try:
        await deps.mail(request).delete_message(OPERATOR_ID, message_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="message not found") from None
    except MailError as exc:
        raise _fail(exc) from exc


@router.get("/messages/{message_id}/suggestions", response_model=list[ReplySuggestionOut])
async def reply_suggestions(
    message_id: str, request: Request, count: int = 3
) -> list[ReplySuggestionOut]:
    """`EMAIL-3` — reply drafts in the operator's own voice, from the learned profile and
    the prior exchange with this sender. Already-stored suggestions come back as-is, so
    reopening a message doesn't re-spend model calls; a workspace with no utility model
    bound simply gets none (degraded, not an error)."""
    try:
        drafts = await deps.mail(request).suggest_replies(
            OPERATOR_ID, message_id, count=max(1, min(count, 5))
        )
    except NotFoundError:
        raise HTTPException(status_code=404, detail="message not found") from None
    except MailError as exc:
        raise _fail(exc) from exc
    return [_suggestion_out(d) for d in drafts]


# --- sending -----------------------------------------------------------------


@router.post("/send", response_model=SendResult)
async def send_message(body: SendRequest, request: Request) -> SendResult:
    if not body.to:
        raise HTTPException(status_code=422, detail="a message needs at least one recipient")
    try:
        sent_id = await deps.mail(request).send(
            OPERATOR_ID,
            body.account_id,
            to=body.to,
            subject=body.subject,
            body=body.body,
            cc=body.cc,
            bcc=body.bcc,
        )
    except NotFoundError:
        raise HTTPException(status_code=404, detail="account not found") from None
    except MailError as exc:
        raise _fail(exc) from exc
    return SendResult(message_id=sent_id)


@router.post("/messages/{message_id}/reply", response_model=SendResult)
async def reply_to_message(message_id: str, body: ReplyRequest, request: Request) -> SendResult:
    try:
        sent_id = await deps.mail(request).reply(
            OPERATOR_ID, message_id, body.body, reply_all=body.reply_all
        )
    except NotFoundError:
        raise HTTPException(status_code=404, detail="message not found") from None
    except MailError as exc:
        raise _fail(exc) from exc
    return SendResult(message_id=sent_id)


# --- writing style (EMAIL-3) --------------------------------------------------


@router.get("/style-profile", response_model=StyleProfileOut | None)
async def get_style_profile(request: Request) -> StyleProfileOut | None:
    return _profile_out(await deps.mail(request).profiles.get(OPERATOR_ID))


@router.put("/style-profile", response_model=StyleProfileOut)
async def set_style_profile(body: StyleProfileUpdate, request: Request) -> StyleProfileOut:
    """The operator's own description of their voice. Stored as ``edited``, which is what
    keeps a later learn pass from overwriting it."""
    stored = await deps.mail(request).profiles.set(OPERATOR_ID, body.profile, edited=True)
    out = _profile_out(stored)
    if out is None:
        raise HTTPException(status_code=422, detail="a style profile must not be empty")
    return out


@router.post("/accounts/{account_id}/learn-style", response_model=StyleProfileOut | None)
async def learn_style(account_id: str, request: Request) -> StyleProfileOut | None:
    """Learn the operator's voice from their Sent folder. A hand-edited profile is left
    alone — their own description outranks ours."""
    service = deps.mail(request)
    try:
        await service.learn_style(OPERATOR_ID, account_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="account not found") from None
    except MailError as exc:
        raise _fail(exc) from exc
    return _profile_out(await service.profiles.get(OPERATOR_ID))
