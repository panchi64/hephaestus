"""The mail capability (`EMAIL-1..5`).

One facade (:class:`~services.mail.service.MailService`) over a pluggable provider seam
(:class:`~services.mail.transport.MailTransport`), with the AI parts — triage (`EMAIL-2`)
and the writing-style profile (`EMAIL-3`) — layered on top of the cache rather than the
network.
"""

from __future__ import annotations

from .cache import MessageDetail, MessageView
from .drafts import DraftView, StyleProfileView
from .errors import MailAuthError, MailError, MailUnavailableError, MailUnsupportedError
from .models import (
    AccountSpec,
    MailAddress,
    MailBody,
    MailFolder,
    MailHeader,
    OutgoingMail,
    TransportCapabilities,
)
from .service import AccountView, MailService
from .transport import MailTransport, WatchableTransport

__all__ = [
    "AccountSpec",
    "AccountView",
    "DraftView",
    "MailAddress",
    "MailAuthError",
    "MailBody",
    "MailError",
    "MailFolder",
    "MailHeader",
    "MailService",
    "MailTransport",
    "MailUnavailableError",
    "MailUnsupportedError",
    "MessageDetail",
    "MessageView",
    "OutgoingMail",
    "StyleProfileView",
    "TransportCapabilities",
    "WatchableTransport",
]
