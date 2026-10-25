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
from collections.abc import Mapping
from typing import override

import httpx

from core.exceptions import SSRFError
from core.serde import parse_json
from core.ssrf import assert_public_url
from services.shapes import as_dict, as_int, as_list, as_str, dict_list

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
from .transport import MailTransport

logger = logging.getLogger(__name__)

_TIMEOUT_S = 30.0
_CORE = "urn:ietf:params:jmap:core"
_MAIL = "urn:ietf:params:jmap:mail"
_SUBMISSION = "urn:ietf:params:jmap:submission"

#: One JMAP method call: its name, its arguments and the id its response comes back under.
type Call = tuple[str, dict[str, object], str]

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


class JmapTransport(MailTransport):
    """A :class:`~plugins.mail.services.transport.MailTransport` over JMAP."""

    def __init__(self, spec: AccountSpec, *, client: httpx.AsyncClient | None = None) -> None:
        self._spec: AccountSpec = spec
        self._client: httpx.AsyncClient | None = client
        self._owns_client: bool = client is None
        self._session: dict[str, str] | None = None

    @override
    def capabilities(self) -> TransportCapabilities:
        return TransportCapabilities(idle=False, search=True, move=True, flags=True, threads=True)

    @override
    async def probe(self) -> None:
        _ = await self._load_session()

    @override
    async def list_folders(self) -> list[MailFolder]:
        [response] = await self._call([("Mailbox/get", {"accountId": None, "ids": None}, "m0")])
        folders: list[MailFolder] = []
        for mailbox in dict_list(response.get("list")):
            role = _ROLES.get(str(mailbox.get("role") or "").lower(), ROLE_OTHER)
            folders.append(
                MailFolder(
                    id=str(mailbox["id"]),
                    name=str(mailbox.get("name") or mailbox["id"]),
                    role=role,
                    total=as_int(mailbox.get("totalEmails")),
                    unread=as_int(mailbox.get("unreadEmails")),
                )
            )
        return folders

    @override
    async def list_messages(
        self, folder: str, *, limit: int = 50, since_uid: str | None = None
    ) -> list[MailHeader]:
        # One round trip: query the mailbox, then `#ids` back-references the query's own
        # result into the get — this is the batching JMAP exists for.
        query: dict[str, object] = {
            "accountId": None,
            "filter": {"inMailbox": folder},
            "sort": [{"property": "receivedAt", "isAscending": False}],
            "limit": limit,
            "calculateTotal": False,
        }
        get: dict[str, object] = {
            "accountId": None,
            "#ids": {"resultOf": "q0", "name": "Email/query", "path": "/ids"},
            "properties": _EMAIL_PROPERTIES,
        }
        _query_response, get_response = await self._call(
            [("Email/query", query, "q0"), ("Email/get", get, "g0")]
        )
        headers = [_to_header(email) for email in dict_list(get_response.get("list"))]
        if since_uid is not None:
            # JMAP ids are opaque, not ordered — "since" is resolved by position: stop at
            # the message the caller last saw.
            for index, header in enumerate(headers):
                if header.uid == since_uid:
                    return headers[:index]
        return headers

    @override
    async def fetch(self, folder: str, uid: str) -> MailBody:
        get: dict[str, object] = {
            "accountId": None,
            "ids": [uid],
            "properties": [*_EMAIL_PROPERTIES, "bodyValues", "textBody", "htmlBody"],
            "fetchAllBodyValues": True,
            "maxBodyValueBytes": 1_000_000,
        }
        [response] = await self._call([("Email/get", get, "g0")])
        found = dict_list(response.get("list"))
        if not found:
            raise MailError("that message is no longer on the server")
        email = found[0]
        text, html = _bodies(email)
        return MailBody(header=_to_header(email), text=text, html=html)

    @override
    async def send(self, message: OutgoingMail) -> str:
        identity_id = await self._identity_id()
        draft = _to_jmap_email(self._spec, message, await self._role_mailbox(ROLE_DRAFTS))
        submission: dict[str, object] = {
            "accountId": None,
            "onSuccessDestroyEmail": ["#draft"],
            "create": {
                "s0": {"emailId": "#draft", "identityId": identity_id},
            },
        }
        set_response, submit_response = await self._call(
            [
                ("Email/set", {"accountId": None, "create": {"draft": draft}}, "e0"),
                ("EmailSubmission/set", submission, "s0"),
            ],
            using=(_CORE, _MAIL, _SUBMISSION),
        )
        if submit_response.get("notCreated"):
            raise MailError("the server refused to send the message")
        created_all = as_dict(set_response.get("created")) or {}
        created = as_dict(created_all.get("draft")) or {}
        # The server mints the Message-ID for a JMAP-composed mail; fall back to the
        # email's own id so the caller always has something to record the send by.
        return _first(created.get("messageId")) or str(created.get("id", ""))

    @override
    async def flag(
        self, folder: str, uid: str, *, seen: bool | None = None, flagged: bool | None = None
    ) -> None:
        patch: dict[str, object] = {}
        if seen is not None:
            patch["keywords/$seen"] = True if seen else None
        if flagged is not None:
            patch["keywords/$flagged"] = True if flagged else None
        if not patch:
            return
        _ = await self._call([("Email/set", {"accountId": None, "update": {uid: patch}}, "e0")])

    @override
    async def move(self, folder: str, uid: str, destination: str) -> None:
        if folder == destination:
            # Already where it was asked to go. Worth an explicit branch because the two
            # patch keys below would collapse into one — a dict literal keeps the last —
            # and the removal half would silently vanish, leaving a patch that does
            # nothing while reporting success.
            return
        patch: dict[str, object] = {f"mailboxIds/{folder}": None, f"mailboxIds/{destination}": True}
        _ = await self._call([("Email/set", {"accountId": None, "update": {uid: patch}}, "e0")])

    @override
    async def delete(self, folder: str, uid: str) -> None:
        # Delete means the message is gone from where it was — the caller drops it from
        # the cache on return, so a no-op here reads to the operator as a message that
        # deleted itself and then came back on the next sync. Trashing is the recoverable
        # default, but a message *already* in Trash has no gentler step left: emptying the
        # trash is a destroy, exactly as every mail client treats it.
        trash = await self._role_mailbox(ROLE_TRASH)
        if trash is None or folder == trash:
            _ = await self._call([("Email/set", {"accountId": None, "destroy": [uid]}, "e0")])
            return
        await self.move(folder, uid, trash)

    @override
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

    async def _load_session(self) -> dict[str, str]:
        """Fetch (once) the JMAP session object: the api url and the account id."""
        if self._session is not None:
            return self._session
        url = str(self._spec.config.get("session_url") or "")
        if not url:
            raise MailError("this account has no JMAP session URL configured")
        payload = await self._request("GET", url)
        accounts = as_dict(payload.get("primaryAccounts")) or {}
        account_id = self._spec.config.get("jmap_account_id") or accounts.get(_MAIL)
        if not account_id:
            raise MailError("the JMAP server exposes no mail account for this credential")
        self._session = {"apiUrl": str(payload.get("apiUrl") or url), "accountId": str(account_id)}
        return self._session

    async def _call(
        self, calls: list[Call], *, using: tuple[str, ...] = (_CORE, _MAIL)
    ) -> list[dict[str, object]]:
        """Issue a batch of method calls, returning each response's argument object.

        ``accountId: None`` in a call's arguments is filled in from the session here, so
        no caller repeats it and the account id lives in exactly one place.
        """
        session = await self._load_session()
        for _name, arguments, _call_id in calls:
            if arguments.get("accountId", "missing") is None:
                arguments["accountId"] = session["accountId"]
        payload = await self._request(
            "POST", session["apiUrl"], json={"using": list(using), "methodCalls": calls}
        )
        results: list[dict[str, object]] = []
        for response in as_list(payload.get("methodResponses")) or []:
            entry = as_list(response)
            arguments = as_dict(entry[1]) if entry is not None and len(entry) > 1 else None
            if entry is None or arguments is None:
                raise MailError("the mail server returned an unexpected response")
            if entry[0] == "error":
                raise MailError(f"the mail server rejected the request: {arguments.get('type')}")
            results.append(arguments)
        if len(results) != len(calls):
            raise MailError("the mail server returned an unexpected response")
        return results

    async def _request(self, method: str, url: str, *, json: object = None) -> dict[str, object]:
        # Re-resolved per request, never once at account-add: a hostname that later
        # points at a private address must still be refused.
        try:
            await assert_public_url(url)
        except SSRFError as exc:
            raise MailError(f"that mail server address is not allowed: {exc}") from exc
        client = await self._http()
        try:
            response = await client.request(
                method, url, headers={"Authorization": f"Bearer {self._token()}"}, json=json
            )
        except httpx.HTTPError as exc:
            raise MailUnavailableError(f"could not reach the mail server: {exc}") from exc
        if response.status_code in (401, 403):
            raise MailAuthError("the mail server rejected this account's credentials")
        if response.status_code >= 400:
            raise MailError(f"the mail server returned HTTP {response.status_code}")
        try:
            document = as_dict(parse_json(response.content))
        except ValueError as exc:
            raise MailError("the mail server returned a malformed response") from exc
        if document is None:
            raise MailError("the mail server returned a malformed response")
        return document

    async def _role_mailbox(self, role: str) -> str | None:
        for folder in await self.list_folders():
            if folder.role == role:
                return folder.id
        return None

    async def _identity_id(self) -> str:
        [response] = await self._call(
            [("Identity/get", {"accountId": None, "ids": None}, "i0")],
            using=(_CORE, _MAIL, _SUBMISSION),
        )
        identities = dict_list(response.get("list"))
        for identity in identities:
            if identity.get("email") == self._spec.address:
                return str(identity["id"])
        if not identities:
            raise MailError("the mail server exposes no sending identity for this account")
        return str(identities[0]["id"])


def _address(entry: object) -> MailAddress | None:
    fields = as_dict(entry)
    if not fields or not fields.get("email"):
        return None
    return MailAddress(address=str(fields["email"]), name=as_str(fields.get("name")) or None)


def _addresses(entries: object) -> tuple[MailAddress, ...]:
    found = (_address(entry) for entry in (as_list(entries) or []))
    return tuple(address for address in found if address is not None)


def _to_header(email: Mapping[str, object]) -> MailHeader:
    keywords = as_dict(email.get("keywords")) or {}
    senders = as_list(email.get("from")) or [None]
    sender = _address(senders[0]) or MailAddress(address="unknown@invalid")
    return MailHeader(
        uid=str(email["id"]),
        sender=sender,
        subject=str(email.get("subject") or ""),
        received_at=parse_utc(email.get("receivedAt")),
        to=_addresses(email.get("to")),
        cc=_addresses(email.get("cc")),
        snippet=snippet_of(str(email.get("preview") or "")),
        thread_id=as_str(email.get("threadId")),
        message_id=_first(email.get("messageId")),
        in_reply_to=_first(email.get("inReplyTo")),
        seen=bool(keywords.get("$seen")),
        flagged=bool(keywords.get("$flagged")),
        has_attachments=bool(email.get("hasAttachment")),
        size_bytes=as_int(email.get("size")),
    )


def _first(value: object) -> str | None:
    items = as_list(value)
    if items and isinstance(value, list):
        return str(items[0])
    return value if isinstance(value, str) else None


def _bodies(email: Mapping[str, object]) -> tuple[str, str | None]:
    """Resolve the body parts a JMAP ``Email/get`` returns by reference into text/HTML."""
    values = as_dict(email.get("bodyValues")) or {}

    def _join(parts: object) -> str:
        chunks = [
            as_str((as_dict(values.get(str(part.get("partId")))) or {}).get("value", "")) or ""
            for part in dict_list(parts)
        ]
        return "\n".join(chunk for chunk in chunks if chunk).strip()

    text = _join(email.get("textBody"))
    html = _join(email.get("htmlBody")) or None
    return (text or html_to_text(html)), html


def _to_jmap_email(
    spec: AccountSpec, message: OutgoingMail, drafts_mailbox: str | None
) -> dict[str, object]:
    """The JMAP ``Email`` object for an outgoing message. Composed as structured JSON
    rather than a serialized RFC 5322 blob — JMAP's own representation, so the server
    does the encoding."""
    email: dict[str, object] = {
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
