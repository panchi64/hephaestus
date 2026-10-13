"""Outlook / Microsoft 365 over the Graph REST API.

Graph is the closest of the three REST-ish providers to a classic mailbox: real folders
with real ids, and a message resource that already carries structured sender, recipients
and body. So unlike Gmail (which hands back raw RFC 5322) this adapter maps Graph's JSON
straight onto the domain models, converting an HTML body through the shared
:mod:`services.mail.parse` reducer so every provider's ``MailBody.text`` means the same
thing downstream.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import httpx

from .errors import MailError
from .models import (
    ROLE_ARCHIVE,
    ROLE_DRAFTS,
    ROLE_INBOX,
    ROLE_OTHER,
    ROLE_SENT,
    ROLE_SPAM,
    ROLE_TRASH,
    AccountSpec,
    MailAddress,
    MailBody,
    MailFolder,
    MailHeader,
    OutgoingMail,
    TransportCapabilities,
)
from .parse import html_to_text, parse_utc, snippet_of
from .rest import RestApi

_BASE_URL = "https://graph.microsoft.com/v1.0/me"

# Graph's well-known folder names → our normalized roles.
_ROLES = {
    "inbox": ROLE_INBOX,
    "sentitems": ROLE_SENT,
    "drafts": ROLE_DRAFTS,
    "deleteditems": ROLE_TRASH,
    "junkemail": ROLE_SPAM,
    "archive": ROLE_ARCHIVE,
}

_LIST_FIELDS = (
    "id,conversationId,internetMessageId,subject,from,toRecipients,ccRecipients,"
    "receivedDateTime,isRead,flag,hasAttachments,bodyPreview"
)


class GraphTransport:
    """A :class:`~services.mail.transport.MailTransport` over Microsoft Graph."""

    def __init__(self, spec: AccountSpec, *, client: httpx.AsyncClient | None = None) -> None:
        self._spec = spec
        self._api = RestApi(spec, _BASE_URL, client=client)

    def capabilities(self) -> TransportCapabilities:
        return TransportCapabilities(idle=False, search=True, move=True, flags=True, threads=True)

    async def probe(self) -> None:
        await self._api.request("GET", "")

    async def list_folders(self) -> list[MailFolder]:
        payload = await self._api.request("GET", "mailFolders", params={"$top": "100"})
        folders = []
        for folder in payload.get("value", []):
            name = str(folder.get("displayName") or folder["id"])
            folders.append(
                MailFolder(
                    id=str(folder["id"]),
                    name=name,
                    role=_ROLES.get(name.replace(" ", "").lower(), ROLE_OTHER),
                    total=folder.get("totalItemCount"),
                    unread=folder.get("unreadItemCount"),
                )
            )
        return folders

    async def list_messages(
        self, folder: str, *, limit: int = 50, since_uid: str | None = None
    ) -> list[MailHeader]:
        payload = await self._api.request(
            "GET",
            f"mailFolders/{folder}/messages",
            params={
                "$top": str(limit),
                "$orderby": "receivedDateTime desc",
                "$select": _LIST_FIELDS,
            },
        )
        headers: list[MailHeader] = []
        for message in payload.get("value", []):
            header = _to_header(message)
            if since_uid is not None and header.uid == since_uid:
                break  # ordered newest-first — stop at what the caller last saw.
            headers.append(header)
        return headers

    async def fetch(self, folder: str, uid: str) -> MailBody:
        message = await self._api.request("GET", f"messages/{uid}")
        if not message:
            raise MailError("that message is no longer on the server")
        body = message.get("body") or {}
        content = str(body.get("content") or "")
        is_html = str(body.get("contentType") or "").lower() == "html"
        text = html_to_text(content) if is_html else content
        return MailBody(
            header=_to_header(message),
            text=text.strip(),
            html=content if is_html else None,
            attachments=(),
        )

    async def send(self, message: OutgoingMail) -> str:
        payload = {
            "message": {
                "subject": message.subject,
                "body": {"contentType": "Text", "content": message.body},
                "toRecipients": [_recipient(a) for a in message.to],
                "ccRecipients": [_recipient(a) for a in message.cc],
                "bccRecipients": [_recipient(a) for a in message.bcc],
            },
            "saveToSentItems": True,
        }
        if message.in_reply_to:
            # Graph has no In-Reply-To field on `sendMail`; the internet message headers
            # extension is how a reply is threaded without fetching the parent first.
            payload["message"]["internetMessageHeaders"] = [
                {"name": "In-Reply-To", "value": message.in_reply_to},
                {
                    "name": "References",
                    "value": " ".join([*message.references, message.in_reply_to]),
                },
            ]
        await self._api.request("POST", "sendMail", json=payload)
        # Graph mints the Message-ID server-side and `sendMail` returns nothing, so there
        # is genuinely no id to hand back — better an honest empty string than a fabricated
        # one the caller might record as the sent message's identity.
        return ""

    async def flag(
        self, folder: str, uid: str, *, seen: bool | None = None, flagged: bool | None = None
    ) -> None:
        patch: dict[str, Any] = {}
        if seen is not None:
            patch["isRead"] = seen
        if flagged is not None:
            patch["flag"] = {"flagStatus": "flagged" if flagged else "notFlagged"}
        if not patch:
            return
        await self._api.request("PATCH", f"messages/{uid}", json=patch)

    async def move(self, folder: str, uid: str, destination: str) -> None:
        await self._api.request("POST", f"messages/{uid}/move", json={"destinationId": destination})

    async def delete(self, folder: str, uid: str) -> None:
        # Graph's DELETE moves to Deleted Items rather than erasing — recoverable, which
        # matches how the other adapters behave.
        await self._api.request("DELETE", f"messages/{uid}")

    async def close(self) -> None:
        await self._api.close()


def _recipient(address: MailAddress) -> dict[str, Any]:
    return {"emailAddress": {"address": address.address, "name": address.name or ""}}


def _address(entry: Any) -> MailAddress | None:
    email = (entry or {}).get("emailAddress") or {}
    if not email.get("address"):
        return None
    return MailAddress(address=str(email["address"]), name=email.get("name") or None)


def _addresses(entries: Any) -> tuple[MailAddress, ...]:
    found = (_address(entry) for entry in (entries or []))
    return tuple(address for address in found if address is not None)


def _to_header(message: dict[str, Any]) -> MailHeader:
    sender = _address(message.get("from")) or MailAddress(address="unknown@invalid")
    flag_status = str((message.get("flag") or {}).get("flagStatus") or "")
    header = MailHeader(
        uid=str(message["id"]),
        sender=sender,
        subject=str(message.get("subject") or ""),
        received_at=parse_utc(message.get("receivedDateTime")),
        to=_addresses(message.get("toRecipients")),
        cc=_addresses(message.get("ccRecipients")),
        snippet=snippet_of(str(message.get("bodyPreview") or "")),
        thread_id=message.get("conversationId"),
        message_id=message.get("internetMessageId"),
        seen=bool(message.get("isRead")),
        flagged=flag_status == "flagged",
        has_attachments=bool(message.get("hasAttachments")),
    )
    return replace(header, size_bytes=message.get("size"))
