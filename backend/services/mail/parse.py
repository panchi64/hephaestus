"""RFC 5322 parsing and composition — the one place raw mail becomes domain models.

Parsing is stdlib ``email`` under ``email.policy.default``: the modern policy already
decodes RFC 2047 encoded words, normalizes headers, and gives structured address objects,
so none of that is re-derived here. Composition goes the same way round — an
``EmailMessage`` built by the stdlib, which the SMTP/REST transports serialize.

An HTML-only body is reduced to text with **trafilatura**, the extractor the web-fetch
capability already uses — one HTML-to-text implementation in the codebase, not two.
"""

from __future__ import annotations

import re
from datetime import datetime
from email import message_from_bytes, policy
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid, parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any

import trafilatura

from models._fields import utcnow

from .models import MailAddress, MailBody, MailHeader, OutgoingMail

# How much of the body the listing preview carries. Long enough to be useful in a list
# row, short enough that a full inbox page stays small.
SNIPPET_CHARS = 200

_WHITESPACE = re.compile(r"\s+")


def parse_message(raw: bytes, *, uid: str, thread_id: str | None = None) -> MailBody:
    """Parse one RFC 5322 message into a :class:`MailBody`."""
    message = message_from_bytes(raw, policy=policy.default)
    text, html = _bodies(message)
    body_text = text or (html_to_text(html) if html else "")
    header = MailHeader(
        uid=uid,
        sender=_first_address(message, "From") or MailAddress(address="unknown@invalid"),
        subject=_header_str(message, "Subject"),
        received_at=_received_at(message),
        to=_addresses(message, "To"),
        cc=_addresses(message, "Cc"),
        snippet=snippet_of(body_text),
        thread_id=thread_id,
        message_id=_header_str(message, "Message-ID") or None,
        in_reply_to=_header_str(message, "In-Reply-To") or None,
        has_attachments=any(_attachment_names(message)),
        size_bytes=len(raw),
    )
    return MailBody(
        header=header, text=body_text, html=html, attachments=tuple(_attachment_names(message))
    )


def html_to_text(html: str | None) -> str:
    """Reduce an HTML body to readable plain text, via the extractor `services/webfetch`
    already depends on, with a plain tag-strip behind it.

    The fallback earns its place here in a way it doesn't for web pages: trafilatura is
    tuned to find the *main article* of a document and discards everything else, which is
    right for a news page and wrong for a three-sentence mail with no article structure —
    or for the table-soup a marketing sender produces. When it finds nothing, the body is
    stripped of markup rather than shown as empty. Markup never escapes either path.
    """
    if not html:
        return ""
    try:
        extracted = trafilatura.extract(
            html, output_format="markdown", include_tables=True, favor_recall=True
        )
    except Exception:  # noqa: BLE001 — extraction is best-effort; markup must not escape
        extracted = None
    return (extracted or "").strip() or _strip_tags(html)


class _TextExtractor(HTMLParser):
    """A minimal tag-stripper: text only, block elements separated by newlines, and the
    contents of ``script``/``style`` dropped entirely."""

    _SKIP = {"script", "style", "head", "title"}
    _BREAK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self._SKIP:
            self._skipping += 1
        elif tag in self._BREAK:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skipping:
            self._skipping -= 1
        elif tag in self._BREAK:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            self._chunks.append(data)

    @property
    def text(self) -> str:
        joined = "".join(self._chunks)
        lines = [_WHITESPACE.sub(" ", line).strip() for line in joined.splitlines()]
        return "\n".join(line for line in lines if line).strip()


def _strip_tags(html: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 — malformed markup must not lose the message
        return ""
    return parser.text


def snippet_of(text: str, limit: int = SNIPPET_CHARS) -> str:
    """A single-line preview of ``text``, whitespace-collapsed and capped."""
    collapsed = _WHITESPACE.sub(" ", text).strip()
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1].rstrip() + "…"


def parse_utc(value: Any) -> datetime | None:
    """An ISO-8601 timestamp from a JSON mail API, or ``None`` if it is absent or junk.

    Shared by the Graph and JMAP adapters, which had identical private copies. Both
    APIs spell UTC as a trailing ``Z``, which ``fromisoformat`` did not accept before
    3.11 and which is normalized here so the two adapters cannot drift apart on it.
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def build_outgoing(sender: MailAddress, message: OutgoingMail) -> EmailMessage:
    """Compose ``message`` into an ``EmailMessage`` ready for SMTP or a REST send.

    A Message-ID is minted here rather than left to the server, so the caller can record
    what it sent and thread a follow-up against it.
    """
    out = EmailMessage()
    out["From"] = _address_header(sender)
    out["To"] = ", ".join(a.format() for a in message.to)
    if message.cc:
        out["Cc"] = ", ".join(a.format() for a in message.cc)
    if message.bcc:
        out["Bcc"] = ", ".join(a.format() for a in message.bcc)
    out["Subject"] = message.subject
    out["Message-ID"] = make_msgid()
    if message.in_reply_to:
        out["In-Reply-To"] = message.in_reply_to
        # References carries the full ancestry; the parent's own id must terminate it or
        # clients thread the reply as a new conversation.
        chain = [*message.references, message.in_reply_to]
        out["References"] = " ".join(dict.fromkeys(chain))
    out["Date"] = format_datetime(utcnow())
    out.set_content(message.body)
    return out


def _address_header(address: MailAddress) -> Address:
    local, _, domain = address.address.partition("@")
    return Address(display_name=address.name or "", username=local, domain=domain)


def _bodies(message: Any) -> tuple[str | None, str | None]:
    """The plain-text and HTML bodies, either of which may be absent."""
    text_part = message.get_body(preferencelist=("plain",))
    html_part = message.get_body(preferencelist=("html",))
    return _part_text(text_part), _part_text(html_part)


def _part_text(part: Any) -> str | None:
    if part is None:
        return None
    try:
        content = part.get_content()
    except LookupError, UnicodeDecodeError, KeyError:
        # An unknown charset or a broken transfer encoding — read the raw payload with a
        # replacement decode rather than dropping the message entirely.
        payload = part.get_payload(decode=True)
        content = payload.decode("utf-8", "replace") if payload else ""
    return content if isinstance(content, str) else None


def _attachment_names(message: Any) -> list[str]:
    return [part.get_filename() or "attachment" for part in message.iter_attachments()]


def _header_str(message: Any, name: str) -> str:
    value = message.get(name)
    return str(value).strip() if value is not None else ""


def _received_at(message: Any):
    raw = message.get("Date")
    if raw is None:
        return None
    try:
        return parsedate_to_datetime(str(raw))
    except TypeError, ValueError:
        return None


def _addresses(message: Any, name: str) -> tuple[MailAddress, ...]:
    header = message.get(name)
    if header is None:
        return ()
    found: list[MailAddress] = []
    for address in getattr(header, "addresses", ()):
        if address.addr_spec:
            found.append(MailAddress(address=address.addr_spec, name=address.display_name or None))
    return tuple(found)


def _first_address(message: Any, name: str) -> MailAddress | None:
    addresses = _addresses(message, name)
    return addresses[0] if addresses else None
