"""Gmail over its REST API.

Gmail's model is labels, not folders: a message carries a set of label ids and "moving"
it means swapping one label for another. The adapter presents that as folders anyway —
the label *is* the folder id — so nothing above the seam has to know, which is the point
of the transport being a Protocol.

Reading uses ``format=RAW``: Gmail hands back the original RFC 5322 bytes, so the message
goes through the same :mod:`plugins.mail.services.parse` layer as an IMAP fetch. One parser for
every provider, and the quoted/signature split (`EMAIL-4`) behaves identically.
"""

from __future__ import annotations

import base64
from dataclasses import replace
from datetime import UTC, datetime
from typing import override

import httpx

from core.concurrency import gather_bounded
from services.shapes import as_int, as_list, as_str, dict_list

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
from .parse import build_outgoing, parse_message, snippet_of
from .rest import RestApi
from .transport import MailTransport

_BASE_URL = "https://gmail.googleapis.com/gmail/v1/users/me"

# How many per-message reads a listing keeps in flight. Gmail's per-user rate limit is
# generous but real, and this is one operator's mailbox, not a crawler: enough to collapse
# a page's round trips into a handful, low enough not to look like a burst.
_LIST_CONCURRENCY = 8

# Gmail's system label ids → our normalized roles.
_ROLES = {
    "INBOX": ROLE_INBOX,
    "SENT": ROLE_SENT,
    "DRAFT": ROLE_DRAFTS,
    "TRASH": ROLE_TRASH,
    "SPAM": ROLE_SPAM,
    "CATEGORY_PERSONAL": ROLE_OTHER,
}


class GmailTransport(MailTransport):
    """A :class:`~plugins.mail.services.transport.MailTransport` over the Gmail REST API."""

    def __init__(self, spec: AccountSpec, *, client: httpx.AsyncClient | None = None) -> None:
        self._spec: AccountSpec = spec
        self._api: RestApi = RestApi(spec, _BASE_URL, client=client)

    @override
    def capabilities(self) -> TransportCapabilities:
        return TransportCapabilities(idle=False, search=True, move=True, flags=True, threads=True)

    @override
    async def probe(self) -> None:
        _ = await self._api.request("GET", "profile")

    @override
    async def list_folders(self) -> list[MailFolder]:
        payload = await self._api.request("GET", "labels")
        folders: list[MailFolder] = []
        for label in dict_list(payload.get("labels")):
            label_id = str(label["id"])
            folders.append(
                MailFolder(
                    id=label_id,
                    name=str(label.get("name") or label_id),
                    role=_ROLES.get(label_id, ROLE_ARCHIVE if label_id == "ALL" else ROLE_OTHER),
                    total=as_int(label.get("messagesTotal")),
                    unread=as_int(label.get("messagesUnread")),
                )
            )
        return folders

    @override
    async def list_messages(
        self, folder: str, *, limit: int = 50, since_uid: str | None = None
    ) -> list[MailHeader]:
        params = {"labelIds": folder, "maxResults": str(limit)}
        listing = await self._api.request("GET", "messages", params=params)
        wanted: list[str] = []
        for stub in dict_list(listing.get("messages")):
            message_id = str(stub["id"])
            if since_uid is not None and message_id == since_uid:
                break  # Gmail lists newest-first — stop at what the caller last saw.
            wanted.append(message_id)
        # Gmail's list endpoint returns id stubs only, so a page of headers costs one
        # request per message no matter what. What it need not cost is one *round trip* per
        # message: awaited in a loop, a 50-message page was fifty sequential round trips to
        # Google. The window is applied before fetching (rather than breaking mid-loop) so
        # the concurrent form reads exactly the messages the serial form would have, and
        # `gather_bounded` preserves listing order.
        bodies = await gather_bounded([self._read(mid) for mid in wanted], _LIST_CONCURRENCY)
        return [body.header for body in bodies]

    @override
    async def fetch(self, folder: str, uid: str) -> MailBody:
        return await self._read(uid)

    @override
    async def send(self, message: OutgoingMail) -> str:
        sender = MailAddress(address=self._spec.address)
        composed = build_outgoing(sender, message)
        # Threading rides the In-Reply-To/References headers `build_outgoing` already
        # composed — Gmail's own `threadId` is a different identifier space than the RFC
        # Message-IDs a caller has, so the standard headers are the portable route.
        payload = {"raw": _b64url_encode(composed.as_bytes())}
        _ = await self._api.request("POST", "messages/send", json=payload)
        return str(composed.get("Message-ID"))

    @override
    async def flag(
        self, folder: str, uid: str, *, seen: bool | None = None, flagged: bool | None = None
    ) -> None:
        add: list[str] = []
        remove: list[str] = []
        if seen is not None:
            (remove if seen else add).append("UNREAD")
        if flagged is not None:
            (add if flagged else remove).append("STARRED")
        if not add and not remove:
            return
        _ = await self._api.request(
            "POST",
            f"messages/{uid}/modify",
            json={"addLabelIds": add, "removeLabelIds": remove},
        )

    @override
    async def move(self, folder: str, uid: str, destination: str) -> None:
        _ = await self._api.request(
            "POST",
            f"messages/{uid}/modify",
            json={"addLabelIds": [destination], "removeLabelIds": [folder]},
        )

    @override
    async def delete(self, folder: str, uid: str) -> None:
        # `trash`, not `delete`: the operator's mail should be recoverable, and Gmail's
        # hard delete is irreversible.
        _ = await self._api.request("POST", f"messages/{uid}/trash")

    @override
    async def close(self) -> None:
        await self._api.close()

    # --- internals -------------------------------------------------------------

    async def _read(self, message_id: str) -> MailBody:
        payload = await self._api.request("GET", f"messages/{message_id}", params={"format": "RAW"})
        raw = payload.get("raw")
        if not raw:
            raise MailError("that message is no longer on the server")
        body = parse_message(
            _b64url_decode(str(raw)), uid=message_id, thread_id=as_str(payload.get("threadId"))
        )
        labels = {str(label) for label in as_list(payload.get("labelIds")) or ()}
        snippet = snippet_of(str(payload.get("snippet") or "")) or body.header.snippet
        header = _stamp(
            body.header,
            seen="UNREAD" not in labels,
            flagged="STARRED" in labels,
            snippet=snippet,
            received_at=_internal_date(payload.get("internalDate")),
            size_bytes=as_int(payload.get("sizeEstimate")),
        )
        return MailBody(header=header, text=body.text, html=body.html, attachments=body.attachments)


def _stamp(
    header: MailHeader,
    *,
    seen: bool,
    flagged: bool,
    snippet: str,
    received_at: datetime | None,
    size_bytes: int | None,
) -> MailHeader:
    """Overlay provider-authoritative fields onto a parsed header, ignoring absent ones."""
    overrides: dict[str, object] = {
        "seen": seen,
        "flagged": flagged,
        "snippet": snippet,
        "received_at": received_at,
        "size_bytes": size_bytes,
    }
    clean = {key: value for key, value in overrides.items() if value is not None}
    return replace(header, **clean)


def _internal_date(value: object) -> datetime | None:
    """Gmail's ``internalDate`` is milliseconds since the epoch — more reliable than the
    ``Date`` header, which the sender controls."""
    if not isinstance(value, str | int | float):
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=UTC)
    except ValueError:
        return None


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode()


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
