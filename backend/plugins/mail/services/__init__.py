"""The mail capability.

One facade (:class:`~plugins.mail.services.service.MailService`) over a pluggable provider seam
(:class:`~plugins.mail.services.transport.MailTransport`), with the AI parts — triage
and the writing-style profile — layered on top of the cache rather than the
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
