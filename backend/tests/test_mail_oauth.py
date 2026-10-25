"""OAuth custody for mail accounts: a bundle is opened only through the vault, an
expiring access token refreshes transparently, and the rotated result is **re-sealed and
persisted before it is handed out** — never left unsealed or only in memory."""

from __future__ import annotations

import json
from pathlib import Path
from typing import NoReturn
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import Engine
from sqlmodel import Session, select

from core.db import in_session, init_db, make_engine
from core.vault import Vault
from models.mail import AUTH_OAUTH, AUTH_PASSWORD, MailAccount
from services.credential_store import CredentialStore, known_services
from services.mail.errors import MailAuthError, MailUnavailableError
from services.mail.oauth import (
    PROVIDERS,
    MailSecrets,
    OAuthProvider,
    TokenBundle,
    authorization_url,
)

NOW = 1_800_000_000.0
# Wall-clock-relative fixtures: `open_access` judges expiry against the real clock.
EXPIRED = 1_000_000_000.0  # long past
LIVE = 4_000_000_000.0  # far future


type _Wired = tuple[Engine, Vault, MailSecrets]


@pytest.fixture
async def wired(tmp_path: Path) -> _Wired:
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(tmp_path / "keyfile.json")
    await vault.setup("pw")
    credentials = CredentialStore(engine, vault)
    _ = await credentials.set_key("operator", "google_oauth", "client-secret")
    return engine, vault, MailSecrets(engine, vault, credentials)


async def _account(engine: Engine, vault: Vault, **overrides: str | None) -> MailAccount:
    account = MailAccount(
        owner_id="operator",
        name="Personal",
        address_enc=vault.encrypt_str("operator@example.com"),
        provider="gmail",
        auth_kind=AUTH_OAUTH,
        config={"oauth_provider": "google", "oauth_client_id": "client-id"},
    )
    for key, value in overrides.items():
        setattr(account, key, value)

    def work(session: Session) -> None:
        session.add(account)

    await in_session(engine, work)
    return account


def test_the_oauth_client_registrations_are_in_the_declared_catalog():
    ids = {service.id for service in known_services()}
    assert {"google_oauth", "microsoft_oauth"} <= ids
    # Every provider's declared credential service must actually exist in the catalog.
    assert all(provider.credential_service in ids for provider in PROVIDERS.values())


def test_the_consent_url_asks_for_a_refresh_token():
    url = authorization_url(
        PROVIDERS["google"],
        client_id="client-id",
        redirect_uri="http://localhost:8000/mail/oauth/callback",
        state="state-1",
    )
    params = parse_qs(urlparse(url).query)
    # Without offline access + a forced consent, Google returns no refresh token and the
    # account silently stops syncing once the first access token lapses.
    assert params["access_type"] == ["offline"]
    assert params["prompt"] == ["consent"]
    assert params["state"] == ["state-1"]
    assert "gmail.modify" in params["scope"][0]


def test_a_bundle_round_trips_through_json():
    bundle = TokenBundle(access_token="at", refresh_token="rt", expires_at=NOW, scope="s")
    assert TokenBundle.from_json(bundle.to_json()) == bundle


def test_expiry_is_judged_with_a_margin():
    bundle = TokenBundle(access_token="at", expires_at=NOW)
    assert bundle.expiring(now=NOW - 60) is True  # inside the refresh margin
    assert bundle.expiring(now=NOW - 3600) is False
    assert TokenBundle(access_token="at").expiring(now=NOW) is False  # no expiry known


async def test_a_password_account_opens_to_its_password(wired: _Wired):
    engine, vault, secrets = wired
    account = await _account(
        engine,
        vault,
        auth_kind=AUTH_PASSWORD,
        provider="imap",
        secret_enc=secrets.seal_password("hunter2"),
    )
    assert await secrets.open_access(account) == ("hunter2", None)


async def test_a_live_token_is_returned_without_contacting_the_provider(
    wired: _Wired, monkeypatch: pytest.MonkeyPatch
):
    engine, vault, secrets = wired
    bundle = TokenBundle(access_token="live", refresh_token="rt", expires_at=LIVE)
    account = await _account(engine, vault, secret_enc=secrets.seal_bundle(bundle))

    async def _never(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("a live token must not trigger a refresh")

    monkeypatch.setattr("services.mail.oauth.refresh_tokens", _never)
    assert await secrets.open_access(account) == (None, "live")


async def test_an_expiring_token_refreshes_and_is_resealed(
    wired: _Wired, monkeypatch: pytest.MonkeyPatch
):
    engine, vault, secrets = wired
    stale = TokenBundle(access_token="stale", refresh_token="rt-1", expires_at=EXPIRED)
    account = await _account(engine, vault, secret_enc=secrets.seal_bundle(stale))
    seen: dict[str, object] = {}

    async def _refresh(
        provider: OAuthProvider, *, client_id: str, client_secret: str, refresh_token: str
    ) -> TokenBundle:
        seen.update(
            provider=provider.id,
            client_id=client_id,
            client_secret=client_secret,
            refresh_token=refresh_token,
        )
        # Google rotates the refresh token on every use — the new one must survive.
        return TokenBundle(access_token="fresh", refresh_token="rt-2", expires_at=LIVE)

    monkeypatch.setattr("services.mail.oauth.refresh_tokens", _refresh)
    password, token = await secrets.open_access(account)

    assert (password, token) == (None, "fresh")
    assert seen == {
        "provider": "google",
        "client_id": "client-id",
        "client_secret": "client-secret",
        "refresh_token": "rt-1",
    }

    def read(session: Session) -> str | None:
        row = session.exec(select(MailAccount).where(MailAccount.id == account.id)).one()
        return row.secret_enc

    stored = await in_session(engine, read)
    # Persisted, and persisted *sealed* — the rotated refresh token is never in the clear.
    assert stored is not None
    assert "rt-2" not in stored
    assert json.loads(vault.decrypt_str(stored))["refresh_token"] == "rt-2"


async def test_a_dead_refresh_token_asks_the_operator_to_reconnect(wired: _Wired):
    engine, vault, secrets = wired
    account = await _account(
        engine,
        vault,
        secret_enc=secrets.seal_bundle(TokenBundle(access_token="x", expires_at=EXPIRED)),
    )
    with pytest.raises(MailAuthError):
        _ = await secrets.open_access(account)


async def test_a_missing_oauth_client_is_reported_as_setup_not_failure(tmp_path: Path):
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(tmp_path / "bare.json")
    await vault.setup("pw")
    # No client secret stored at all — the operator hasn't been to the API Tokens page.
    secrets = MailSecrets(engine, vault, CredentialStore(engine, vault))
    account = await _account(
        engine,
        vault,
        secret_enc=secrets.seal_bundle(
            TokenBundle(access_token="x", refresh_token="rt", expires_at=EXPIRED)
        ),
    )
    with pytest.raises(MailAuthError, match="API Tokens"):
        _ = await secrets.open_access(account)


async def test_a_locked_vault_parks_rather_than_failing_hard(wired: _Wired):
    engine, vault, secrets = wired
    account = await _account(
        engine, vault, secret_enc=secrets.seal_bundle(TokenBundle(access_token="x"))
    )
    vault.lock()
    with pytest.raises(MailUnavailableError):
        _ = await secrets.open_access(account)
