"""Mail domain errors.

The capability raises these and never ``HTTPException`` — the routes map them, the tools
decide retry-vs-degrade. They live beside the capability (like ``services.sandbox``'s
``SandboxError``) rather than in ``core.exceptions``, because only mail callers handle them.
"""

from __future__ import annotations

from core.exceptions import HephaestusError


class MailError(HephaestusError):
    """A mail operation failed in a way the operator or the model can act on — a refused
    login, an unreachable server, a message the provider no longer has. Carries a plain
    sentence; **never** a password, token, or message content."""


class MailAuthError(MailError):
    """The provider rejected the account's credentials. For an OAuth account this means
    the refresh token is dead and the operator must reconnect; for a password account,
    the stored password is wrong. Distinguished so the routes can say which."""


class MailUnavailableError(MailError):
    """The account can't be reached right now — the server is down, the network is out, or
    the vault is locked so the secret can't be opened. A transient precondition the caller
    degrades on (serve the cache, park the sync) rather than an operator error."""


class MailUnsupportedError(MailError):
    """The account's provider does not support the requested operation (e.g. moving a
    message on a transport that only labels). Reported from
    :class:`~plugins.mail.services.models.TransportCapabilities`, not discovered by failing."""
