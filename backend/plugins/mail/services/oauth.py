"""OAuth2 for mail accounts — consent, code exchange, refresh, and sealed custody.

Google and Microsoft don't accept a password for mail any more; both want an OAuth2
authorization-code grant with a long-lived refresh token. Two distinct secrets are
involved and they live in **different places on purpose**:

- The **client registration** (client id + secret) is one per *install*, identifies this
  copy of Hephaestus to Google/Microsoft, and is the same no matter how many mailboxes the
  operator connects. That is exactly what ``services/credential_store``'s static catalog
  models, so it lives there, on the API Tokens surface.
- The **per-account token bundle** (access token, refresh token, expiry, scope) is one per
  *mailbox* and rotates. It lives sealed on the account row (``MailAccount.secret_enc``).

:class:`MailSecrets` owns that second half end to end: opening a bundle, noticing an
access token is about to expire, refreshing it, and **re-sealing the result before
returning it** — so a provider that rotates the refresh token (Google does) never leaves
the new one unsealed or, worse, only in memory to be lost on restart.

The exchange itself is ``authlib``'s async OAuth2 client, which since authlib 1.8 is an
``httpx2.AsyncClient`` subclass rather than an ``httpx`` one — a **different** client stack
from the pooled ``httpx`` client the rest of this app shares (``core/net.py``), and one it
builds for itself per exchange. That costs nothing here: an OAuth exchange is rare, brief,
and to a fixed, well-known token endpoint rather than operator input, so it is neither worth
pooling nor an SSRF surface the way a JMAP server URL is. Nothing is passed to the
constructor, which is what keeps this indifferent to which generation authlib rides.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import timedelta

from authlib.integrations.httpx_client import AsyncOAuth2Client
from sqlalchemy import Engine
from sqlmodel import Session

from core.db import in_session
from core.serde import parse_json
from core.vault import Vault, VaultLocked
from models._fields import utcnow
from services.credential_store import CredentialStore, ServiceInfo
from services.shapes import as_dict, as_number, as_str

from ..models import AUTH_OAUTH, MailAccount
from .errors import MailAuthError, MailUnavailableError

logger = logging.getLogger(__name__)

# Refresh this far ahead of expiry, so a token can't lapse mid-request.
_REFRESH_MARGIN = timedelta(minutes=5)


@dataclass(frozen=True, slots=True)
class OAuthProvider:
    """One identity provider's fixed endpoints and the scopes mail needs from it."""

    id: str
    label: str
    authorize_url: str
    token_url: str
    scopes: tuple[str, ...]
    # The `OAUTH_CLIENT_SERVICES` id whose stored key is this provider's client secret. The
    # client id is not a secret and is held beside it in the account's config.
    credential_service: str


#: The OAuth *client* registrations mail reads a stored secret for, one per install rather
#: than per mailbox. The rotating per-account token bundle is a different secret, sealed on
#: the account row (`MailAccount.secret_enc`).
OAUTH_CLIENT_SERVICES: tuple[ServiceInfo, ...] = (
    ServiceInfo(
        "google_oauth",
        "Google (OAuth client)",
        (
            "Gmail accounts — the client secret of your Google OAuth app, used to obtain and "
            "refresh each connected mailbox's access."
        ),
        "https://console.cloud.google.com/apis/credentials",
    ),
    ServiceInfo(
        "microsoft_oauth",
        "Microsoft (OAuth client)",
        (
            "Outlook / Microsoft 365 accounts — the client secret of your Entra app "
            "registration, used to obtain and refresh each connected mailbox's access."
        ),
        "https://entra.microsoft.com/",
    ),
)


PROVIDERS: dict[str, OAuthProvider] = {
    "google": OAuthProvider(
        id="google",
        label="Google",
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        # `gmail.modify` covers read + flag + move; `gmail.send` covers sending. Asking
        # for the narrower pair rather than the blanket `https://mail.google.com/` keeps
        # the grant to what the capability actually does.
        scopes=(
            "https://www.googleapis.com/auth/gmail.modify",
            "https://www.googleapis.com/auth/gmail.send",
        ),
        credential_service="google_oauth",
    ),
    "microsoft": OAuthProvider(
        id="microsoft",
        label="Microsoft",
        authorize_url="https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        token_url="https://login.microsoftonline.com/common/oauth2/v2.0/token",
        scopes=("offline_access", "Mail.ReadWrite", "Mail.Send", "User.Read"),
        credential_service="microsoft_oauth",
    ),
}


@dataclass(frozen=True, slots=True)
class TokenBundle:
    """What the provider hands back, and what gets sealed on the account row."""

    access_token: str
    refresh_token: str | None = None
    expires_at: float | None = None  # POSIX seconds
    scope: str | None = None

    def expiring(self, *, now: float) -> bool:
        """Whether this access token needs refreshing before the next request."""
        if self.expires_at is None:
            return False
        return now >= self.expires_at - _REFRESH_MARGIN.total_seconds()

    def to_json(self) -> str:
        return json.dumps(
            {
                "access_token": self.access_token,
                "refresh_token": self.refresh_token,
                "expires_at": self.expires_at,
                "scope": self.scope,
            }
        )

    @classmethod
    def from_json(cls, raw: str) -> TokenBundle:
        return cls.from_fields(as_dict(parse_json(raw)) or {})

    @classmethod
    def from_fields(cls, data: Mapping[str, object]) -> TokenBundle:
        """A bundle from a token response or a sealed record."""
        return cls(
            access_token=str(data.get("access_token") or ""),
            refresh_token=as_str(data.get("refresh_token")),
            expires_at=as_number(data.get("expires_at")),
            scope=as_str(data.get("scope")),
        )


def authorization_url(
    provider: OAuthProvider, *, client_id: str, redirect_uri: str, state: str
) -> str:
    """The consent URL to send the operator to. ``access_type``/``prompt`` are Google's
    levers for actually issuing a refresh token — without them a re-consent returns an
    access token only and the account silently stops syncing a week later."""
    client = AsyncOAuth2Client(
        client_id=client_id, scope=" ".join(provider.scopes), redirect_uri=redirect_uri
    )
    url, _state = client.create_authorization_url(
        provider.authorize_url, state=state, access_type="offline", prompt="consent"
    )
    return str(url)


async def exchange_code(
    provider: OAuthProvider,
    *,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
) -> TokenBundle:
    """Trade an authorization code for the first token bundle."""
    return await _token_request(
        provider,
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        grant="authorization_code",
        code=code,
    )


async def refresh_tokens(
    provider: OAuthProvider, *, client_id: str, client_secret: str, refresh_token: str
) -> TokenBundle:
    """Exchange a refresh token for a fresh access token (and possibly a rotated
    refresh token — Google rotates, Microsoft usually doesn't, so the old one is kept
    when the response omits it)."""
    bundle = await _token_request(
        provider,
        client_id=client_id,
        client_secret=client_secret,
        grant="refresh_token",
        refresh_token=refresh_token,
    )
    if bundle.refresh_token:
        return bundle
    return replace(bundle, refresh_token=refresh_token)


async def _token_request(
    provider: OAuthProvider,
    *,
    client_id: str,
    client_secret: str,
    grant: str,
    redirect_uri: str | None = None,
    **params: str,
) -> TokenBundle:
    client = AsyncOAuth2Client(
        client_id=client_id, client_secret=client_secret, redirect_uri=redirect_uri
    )
    try:
        async with client:
            token = await client.fetch_token(provider.token_url, grant_type=grant, **params)
    except Exception as exc:  # authlib raises a wide family of errors
        # A refused grant and an unreachable identity provider are different problems for
        # the operator: one needs a reconnect, the other needs waiting.
        message = str(exc).lower()
        if "invalid_grant" in message or "invalid_client" in message or "unauthorized" in message:
            raise MailAuthError(
                "the provider rejected this account's authorization — reconnect it"
            ) from exc
        raise MailUnavailableError(f"could not reach the identity provider: {exc}") from exc
    return TokenBundle.from_fields(token)


class MailSecrets:
    """Sealed custody of a mail account's secret, with OAuth refresh folded in.

    Every read goes through :meth:`open_access`, so there is exactly one path by which a
    token reaches a transport and exactly one place a rotated refresh token is written
    back. The vault stays here — the adapters below receive plain values and never learn
    that encryption exists.
    """

    def __init__(self, engine: Engine, vault: Vault, credentials: CredentialStore) -> None:
        self._engine: Engine = engine
        self._vault: Vault = vault
        self._credentials: CredentialStore = credentials

    def seal_password(self, password: str) -> str:
        return self._vault.encrypt_str(json.dumps({"password": password}))

    def seal_bundle(self, bundle: TokenBundle) -> str:
        return self._vault.encrypt_str(bundle.to_json())

    async def open_access(self, account: MailAccount) -> tuple[str | None, str | None]:
        """``(password, access_token)`` for ``account`` — at most one is set.

        For an OAuth account whose access token is at or near expiry this refreshes it
        against the provider and **re-seals the whole bundle** (rotated refresh token
        included) before returning, so the new secret is durable the moment it exists.
        """
        if not account.secret_enc:
            return None, None
        try:
            raw = self._vault.decrypt_str(account.secret_enc)
        except VaultLocked as exc:
            raise MailUnavailableError(
                "the vault is locked, so this account's credentials cannot be opened"
            ) from exc
        if account.auth_kind != AUTH_OAUTH:
            return as_str((as_dict(parse_json(raw)) or {}).get("password")), None

        bundle = TokenBundle.from_json(raw)
        if not bundle.expiring(now=utcnow().timestamp()):
            return None, bundle.access_token
        return None, await self._refresh_and_reseal(account, bundle)

    async def _refresh_and_reseal(self, account: MailAccount, bundle: TokenBundle) -> str:
        provider = PROVIDERS.get(str(account.config.get("oauth_provider") or ""))
        if provider is None or not bundle.refresh_token:
            raise MailAuthError("this account's authorization has expired — reconnect it")
        client_id = str(account.config.get("oauth_client_id") or "")
        client_secret = await self._credentials.get_secret(
            account.owner_id, provider.credential_service
        )
        if not client_id or not client_secret:
            message = (
                f"no {provider.label} OAuth client is configured — add one on the API "
                "Tokens page before this account can refresh its access"
            )
            raise MailAuthError(message)
        refreshed = await refresh_tokens(
            provider,
            client_id=client_id,
            client_secret=client_secret,
            refresh_token=bundle.refresh_token,
        )
        sealed = self.seal_bundle(refreshed)

        def work(session: Session) -> None:
            row = session.get(MailAccount, account.id)
            if row is not None:
                row.secret_enc = sealed
                row.updated_at = utcnow()
                session.add(row)

        await in_session(self._engine, work)
        account.secret_enc = sealed
        return refreshed.access_token
