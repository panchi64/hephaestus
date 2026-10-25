"""Separating a reply from what it quotes, and from its signature (`EMAIL-4`).

A mail body is usually three things stacked: the sender's new prose, the history they
quoted under it, and a signature block. The reader wants only the first, triage should
summarize only the first, and a generated reply should imitate only the operator's own
signature — so the split is done once, at ingest, and stored on the cached row.

Two layers, cheapest first:

- ``email_reply_parser`` splits the body into fragments and labels each quoted or
  signature. It is battle-tested against the ill-specified reality of quote headers
  ("On … wrote:", ``>`` prefixes, Outlook's ``-----Original Message-----``), which is
  exactly the part not worth re-deriving.
- A small signature pass on top, because the parser only recognizes a signature by the
  RFC 3676 ``-- ``/``__`` delimiter (or "Sent from my …"). When no delimiter is present, a
  short trailing block introduced by a common sign-off is taken as one.

Both are conservative: when nothing matches confidently the whole body stays in ``reply``,
because wrongly hiding real prose is far worse than showing a signature.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from email_reply_parser import EmailReplyParser

logger = logging.getLogger(__name__)

# RFC 3676's signature delimiter — a line that is exactly "--" (optionally with the
# trailing space clients strip), or the "__" some clients use instead.
_SIG_DELIMITER = re.compile(r"^(--|__)\s?$", re.MULTILINE)

# Sign-offs that commonly open a signature block when no "--" delimiter is present.
# Deliberately short and unambiguous — anything looser starts eating real sentences.
_SIGN_OFFS = re.compile(
    (
        r"^\s*(best regards|kind regards|warm regards|best wishes|regards|best|"
        r"cheers|thanks|thank you|sincerely|yours truly|yours sincerely|"
        r"sent from my \w+)[,.!]?\s*$"
    ),
    re.IGNORECASE,
)

# A signature that isn't delimited is only believed when it is short — a long trailing
# block is prose, not a sign-off.
_MAX_SIGNATURE_LINES = 8


@dataclass(frozen=True, slots=True)
class BodyParts:
    """A body split into its three display parts. ``reply`` is always populated (it is
    the whole body when nothing else was found); the other two are ``None`` when absent."""

    reply: str
    quoted: str | None = None
    signature: str | None = None


def split_body(body: str) -> BodyParts:
    """Split ``body`` into new prose, quoted history, and signature (`EMAIL-4`)."""
    if not body.strip():
        return BodyParts(reply="")
    try:
        fragments = EmailReplyParser.read(body).fragments
    except Exception:  # a parser hiccup must never lose the message
        logger.debug("mail: splitting quoted history failed", exc_info=True)
        return BodyParts(reply=body.strip())

    prose: list[str] = []
    quoted: list[str] = []
    signature: list[str] = []
    for fragment in fragments:
        content = fragment.content.strip()
        if not content:
            continue
        if fragment.quoted:
            quoted.append(content)
        elif fragment.signature:
            signature.append(_strip_delimiter(content))
        else:
            prose.append(content)

    reply = "\n\n".join(prose).strip()
    found = "\n\n".join(part for part in signature if part).strip() or None
    if found is None:
        # No delimited signature — try the sign-off heuristic on the prose itself.
        reply, found = _split_signature(reply)
    return BodyParts(reply=reply, quoted="\n\n".join(quoted).strip() or None, signature=found)


def _strip_delimiter(signature: str) -> str:
    """Drop a leading ``-- ``/``__`` delimiter line — it marks the signature, it isn't
    part of it, and showing it back to the reader is noise."""
    lines = signature.splitlines()
    if lines and _SIG_DELIMITER.match(lines[0].rstrip()):
        return "\n".join(lines[1:]).strip()
    return signature.strip()


def _split_signature(text: str) -> tuple[str, str | None]:
    """Peel a trailing signature off ``text``. Returns ``(prose, signature | None)``."""
    delimiters = list(_SIG_DELIMITER.finditer(text))
    if delimiters:
        # The *last* delimiter wins: a quoted message's own signature can leave an
        # earlier one embedded above the sender's.
        cut = delimiters[-1]
        signature = text[cut.end() :].strip()
        return text[: cut.start()].rstrip(), signature or None

    lines = text.splitlines()
    for offset in range(max(0, len(lines) - _MAX_SIGNATURE_LINES), len(lines)):
        if not _SIGN_OFFS.match(lines[offset]):
            continue
        # A sign-off on the very first line is a greeting-shaped body ("Thanks!"), not a
        # signature — there would be nothing left as prose.
        if offset == 0:
            break
        signature = "\n".join(lines[offset:]).strip()
        return "\n".join(lines[:offset]).rstrip(), signature or None
    return text, None
