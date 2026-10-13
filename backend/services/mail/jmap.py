"""JMAP (RFC 8620/8621) over ``httpx`` — the modern, batched mail protocol.

JMAP is a single JSON-RPC-ish endpoint: one POST carries an ordered list of method calls
and comes back with a matching list of responses, with **back-references** (``#ids``) so a
query and the fetch of its results ride in one round trip. That is the whole reason this
adapter exists next to IMAP — a listing costs one request rather than one per message.

The server URL is **operator-supplied**, which makes it an SSRF vector: a "JMAP server" of
``http://169.254.169.254/`` would turn the backend into a proxy for its own cloud metadata.
Every request therefore goes through :func:`core.ssrf.assert_public_url` first — the same
guard web search and the web-fetch browser use — re-resolved per request, so a DNS entry
that flips to a private address after the account was added is still refused.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from core.exceptions import SSRFError
from core.ssrf import assert_public_url

from .errors import MailAuthError, MailError, MailUnavailableError
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

logger = logging.getLogger(__name__)

_TIMEOUT_S = 30.0
_CORE = "urn:ietf:params:jmap:core"
_MAIL = "urn:ietf:params:jmap:mail"
_SUBMISSION = "urn:ietf:params:jmap:submission"

# JMAP mailbox roles (RFC 8621 §2) are already normalized names — map them onto ours.
_ROLES = {
    "inbox": ROLE_INBOX,
    "sent": ROLE_SENT,
    "drafts": ROLE_DRAFTS,
    "trash": ROLE_TRASH,
    "archive": ROLE_ARCHIVE,
    "junk": ROLE_SPAM,
}

_EMAIL_PROPERTIES = [
    "id",
    "blobId",
    "threadId",
    "mailboxIds",
    "keywords",
    "from",
    "to",
    "cc",
    "subject",
    "receivedAt",
    "size",
    "preview",
    "messageId",
    "inReplyTo",
    "hasAttachment",
]


class JmapTransport:
    """A :class:`~services.mail.transport.MailTransport` over JMAP."""

    def __init__(self, spec: AccountSpec, *, client: httpx.AsyncClient | None = None) -> None:
        self._spec = spec
        self._client = client
        self._owns_client = client is None
        self._session: dict[str, Any] | None = None

    def capabilities(self) -> TransportCapabilities:
        return TransportCapabilities(idle=False, search=True, move=True, flags=True, threads=True)

    async def probe(self) -> None:
        await self._load_session()

    async def list_folders(self) -> list[MailFolder]:
        [response] = await self._call([["Mailbox/get", {"accountId": None, "ids": None}, "m0"]])
        folders = []
        for mailbox in response.get("list", []):
            role = _ROLES.get(str(mailbox.get("role") or "").lower(), ROLE_OTHER)
            folders.append(
                MailFolder(
                    id=str(mailbox["id"]),
                    name=str(mailbox.get("name") or mailbox["id"]),
                    role=role,
                    total=mailbox.get("totalEmails"),
                    unread=mailbox.get("unreadEmails"),
                )
            )
        return folders

    async def list_messages(
        self, folder: str, *, limit: int = 50, since_uid: str | None = None
    ) -> list[MailHeader]:
        # One round trip: query the mailbox, then `#ids` back-references the query's own
        # result into the get — this is the batching JMAP exists for.
        query = {
            "accountId": None,
            "filter": {"inMailbox": folder},
            "sort": [{"property": "receivedAt", "isAscending": False}],
            "limit": limit,
            "calculateTotal": False,
        }
        get = {
            "accountId": None,
            "#ids": {"resultOf": "q0", "name": "Email/query", "path": "/ids"},
            "properties": _EMAIL_PROPERTIES,
        }
        _query_response, get_response = await self._call(
            [["Email/query", query, "q0"], ["Email/get", get, "g0"]]
        )
        headers = [_to_header(email) for email in get_response.get("list", [])]
        if since_uid is not None:
            # JMAP ids are opaque, not ordered — "since" is resolved by position: stop at
            # the message the caller last saw.
            for index, header in enumerate(headers):
                if header.uid == since_uid:
                    return headers[:index]
        return headers

    async def fetch(self, folder: str, uid: str) -> MailBody:
        get = {
            "accountId": None,
            "ids": [uid],
            "properties": [*_EMAIL_PROPERTIES, "bodyValues", "textBody", "htmlBody"],
            "fetchAllBodyValues": True,
            "maxBodyValueBytes": 1_000_000,
        }
        [response] = await self._call([["Email/get", get, "g0"]])
        found = response.get("list", [])
        if not found:
            raise MailError("that message is no longer on the server")
        email = found[0]
        text, html = _bodies(email)
        return MailBody(header=_to_header(email), text=text, html=html)

    async def send(self, message: OutgoingMail) -> str:
        identity_id = await self._identity_id()
        draft = _to_jmap_email(self._spec, message, await self._role_mailbox(ROLE_DRAFTS))
        submission = {
            "accountId": None,
            "onSuccessDestroyEmail": ["#draft"],
            "create": {
                "s0": {"emailId": "#draft", "identityId": identity_id},
            },
        }
        set_response, submit_response = await self._call(
            [
                ["Email/set", {"accountId": None, "create": {"draft": draft}}, "e0"],
                ["EmailSubmission/set", submission, "s0"],
            ],
            using=(_CORE, _MAIL, _SUBMISSION),
        )
        if submit_response.get("notCreated"):
            raise MailError("the server refused to send the message")
        created = set_response.get("created", {}).get("draft", {})
        # The server mints the Message-ID for a JMAP-composed mail; fall back to the
        # email's own id so the caller always has something to record the send by.
        return _first(created.get("messageId")) or str(created.get("id", ""))

    async def flag(
        self, folder: str, uid: str, *, seen: bool | None = None, flagged: bool | None = None
    ) -> None:
        patch: dict[str, Any] = {}
        if seen is not None:
            patch["keywords/$seen"] = True if seen else None
        if flagged is not None:
            patch["keywords/$flagged"] = True if flagged else None
        if not patch:
            return
        await self._call([["Email/set", {"accountId": None, "update": {uid: patch}}, "e0"]])

    async def move(self, folder: str, uid: str, destination: str) -> None:
        if folder == destination:
            # Already where it was asked to go. Worth an explicit branch because the two
            # patch keys below would collapse into one — a dict literal keeps the last —
            # and the removal half would silently vanish, leaving a patch that does
            # nothing while reporting success.
            return
        patch = {f"mailboxIds/{folder}": None, f"mailboxIds/{destination}": True}
        await self._call([["Email/set", {"accountId": None, "update": {uid: patch}}, "e0"]])

    async def delete(self, folder: str, uid: str) -> None:
        # Delete means the message is gone from where it was — the caller drops it from
        # the cache on return, so a no-op here reads to the operator as a message that
        # deleted itself and then came back on the next sync. Trashing is the recoverable
        # default, but a message *already* in Trash has no gentler step left: emptying the
        # trash is a destroy, exactly as every mail client treats it.
        trash = await self._role_mailbox(ROLE_TRASH)
        if trash is None or folder == trash:
            await self._call([["Email/set", {"accountId": None, "destroy": [uid]}, "e0"]])
            return
        await self.move(folder, uid, trash)

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
        self._client = None

    # --- internals -------------------------------------------------------------

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=_TIMEOUT_S, follow_redirects=False)
            self._owns_client = True
        return self._client

    def _token(self) -> str:
        token = self._spec.access_token or self._spec.password
        if not token:
            raise MailAuthError("this account has no JMAP credential stored")
        return token

    async def _load_session(self) -> dict[str, Any]:
        """Fetch (once) the JMAP session object: the api url and the account id."""
        if self._session is not None:
            return self._session
        url = str(self._spec.config.get("session_url") or "")
        if not url:
            raise MailError("this account has no JMAP session URL configured")
        payload = await self._request("GET", url)
        accounts = payload.get("primaryAccounts", {})
        account_id = self._spec.config.get("jmap_account_id") or accounts.get(_MAIL)
        if not account_id:
            raise MailError("the JMAP server exposes no mail account for this credential")
        self._session = {"apiUrl": payload.get("apiUrl") or url, "accountId": str(account_id)}
        return self._session

    async def _call(
        self, calls: list[list[Any]], *, using: tuple[str, ...] = (_CORE, _MAIL)
    ) -> list[dict[str, Any]]:
        """Issue a batch of method calls, returning each response's argument object.

        ``accountId: None`` in a call's arguments is filled in from the session here, so
        no caller repeats it and the account id lives in exactly one place.
        """
        session = await self._load_session()
        for call in calls:
            if call[1].get("accountId", "missing") is None:
                call[1]["accountId"] = session["accountId"]
        payload = await self._request(
            "POST", session["apiUrl"], json={"using": list(using), "methodCalls": calls}
        )
        responses = payload.get("methodResponses", [])
        results: list[dict[str, Any]] = []
        for response in responses:
            name, arguments = response[0], response[1]
            if name == "error":
                raise MailError(f"the mail server rejected the request: {arguments.get('type')}")
            results.append(arguments)
        if len(results) != len(calls):
            raise MailError("the mail server returned an unexpected response")
        return results

    async def _request(self, method: str, url: str, **kwargs) -> dict[str, Any]:
        # Re-resolved per request, never once at account-add: a hostname that later
        # points at a private address must still be refused.
        try:
            await assert_public_url(url)
        except SSRFError as exc:
            raise MailError(f"that mail server address is not allowed: {exc}") from exc
        client = await self._http()
        try:
            response = await client.request(
                method, url, headers={"Authorization": f"Bearer {self._token()}"}, **kwargs
            )
        except httpx.HTTPError as exc:
            raise MailUnavailableError(f"could not reach the mail server: {exc}") from exc
        if response.status_code in (401, 403):
            raise MailAuthError("the mail server rejected this account's credentials")
        if response.status_code >= 400:
            raise MailError(f"the mail server returned HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError as exc:
            raise MailError("the mail server returned a malformed response") from exc

    async def _role_mailbox(self, role: str) -> str | None:
        for folder in await self.list_folders():
            if folder.role == role:
                return folder.id
        return None

    async def _identity_id(self) -> str:
        [response] = await self._call(
            [["Identity/get", {"accountId": None, "ids": None}, "i0"]],
            using=(_CORE, _MAIL, _SUBMISSION),
        )
        identities = response.get("list", [])
        for identity in identities:
            if identity.get("email") == self._spec.address:
                return str(identity["id"])
        if not identities:
            raise MailError("the mail server exposes no sending identity for this account")
        return str(identities[0]["id"])


def _address(entry: dict[str, Any] | None) -> MailAddress | None:
    if not entry or not entry.get("email"):
        return None
    return MailAddress(address=str(entry["email"]), name=entry.get("name") or None)


def _addresses(entries: Any) -> tuple[MailAddress, ...]:
    found = (_address(entry) for entry in (entries or []))
    return tuple(address for address in found if address is not None)


def _to_header(email: dict[str, Any]) -> MailHeader:
    keywords = email.get("keywords") or {}
    sender = _address((email.get("from") or [None])[0]) or MailAddress(address="unknown@invalid")
    return MailHeader(
        uid=str(email["id"]),
        sender=sender,
        subject=str(email.get("subject") or ""),
        received_at=parse_utc(email.get("receivedAt")),
        to=_addresses(email.get("to")),
        cc=_addresses(email.get("cc")),
        snippet=snippet_of(str(email.get("preview") or "")),
        thread_id=email.get("threadId"),
        message_id=_first(email.get("messageId")),
        in_reply_to=_first(email.get("inReplyTo")),
        seen=bool(keywords.get("$seen")),
        flagged=bool(keywords.get("$flagged")),
        has_attachments=bool(email.get("hasAttachment")),
        size_bytes=email.get("size"),
    )


def _first(value: Any) -> str | None:
    if isinstance(value, list) and value:
        return str(value[0])
    return str(value) if isinstance(value, str) else None


def _bodies(email: dict[str, Any]) -> tuple[str, str | None]:
    """Resolve the body parts a JMAP ``Email/get`` returns by reference into text/HTML."""
    values = email.get("bodyValues") or {}

    def _join(parts: Any) -> str:
        chunks = [values.get(str(part.get("partId")), {}).get("value", "") for part in parts or []]
        return "\n".join(chunk for chunk in chunks if chunk).strip()

    text = _join(email.get("textBody"))
    html = _join(email.get("htmlBody")) or None
    return (text or html_to_text(html)), html


def _to_jmap_email(
    spec: AccountSpec, message: OutgoingMail, drafts_mailbox: str | None
) -> dict[str, Any]:
    """The JMAP ``Email`` object for an outgoing message. Composed as structured JSON
    rather than a serialized RFC 5322 blob — JMAP's own representation, so the server
    does the encoding."""
    email: dict[str, Any] = {
        "from": [{"email": spec.address}],
        "to": [{"email": a.address, "name": a.name} for a in message.to],
        "subject": message.subject,
        "bodyValues": {"body": {"value": message.body, "isTruncated": False}},
        "textBody": [{"partId": "body", "type": "text/plain"}],
        "keywords": {"$draft": True, "$seen": True},
    }
    if message.cc:
        email["cc"] = [{"email": a.address, "name": a.name} for a in message.cc]
    if message.bcc:
        email["bcc"] = [{"email": a.address, "name": a.name} for a in message.bcc]
    if message.in_reply_to:
        email["inReplyTo"] = [message.in_reply_to]
        email["references"] = [*message.references, message.in_reply_to]
    if drafts_mailbox:
        email["mailboxIds"] = {drafts_mailbox: True}
    return email
