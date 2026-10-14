"""``MailService``'s per-account transport cache, and its one hard rule: a cached adapter
must never outlive the credentials it was built with.

An adapter snapshots its secret into an ``AccountSpec`` at construction and holds an
authenticated connection — it cannot be told that the account's access token rotated. The
service is the only layer that can notice, because ``MailSecrets.open_access`` is both the
sole reader of the sealed secret and the sole refresh path. Short-circuit the cache ahead
of it and every OAuth account fails an hour after its first use.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar, NoReturn, override

import pytest
from sqlalchemy import Engine
from sqlmodel import Session

from core.db import in_session, init_db, make_engine
from core.vault import Vault
from models.mail import AUTH_OAUTH, PROVIDER_GMAIL, MailAccount
from services.credential_store import CredentialStore
from services.mail import service as mail_service
from services.mail.models import AccountSpec
from services.mail.oauth import OAuthProvider, TokenBundle
from services.mail.service import MailService
from services.mail.transport import MailTransport
from tests.mail_fakes import FakeTransport, install_transport

EXPIRED = 1_000_000_000.0  # long past — `open_access` judges expiry against the real clock
LIVE = 4_000_000_000.0  # far future
LIVE_BUNDLE = TokenBundle(access_token="live", refresh_token="rt", expires_at=LIVE)


class _NoModels:
    async def resolve_background(self, **_kwargs: object) -> NoReturn:
        raise RuntimeError("no utility model configured")


class _SpecRecorder(FakeTransport):
    """Stands in for a provider adapter, remembering the spec it was handed."""

    instances: ClassVar[list[_SpecRecorder]] = []

    def __init__(self, spec: AccountSpec | None) -> None:
        super().__init__()
        self.spec: AccountSpec | None = spec
        self.closed: bool = False
        _SpecRecorder.instances.append(self)

    @override
    async def close(self) -> None:
        self.closed = True


def _recorder(transport: MailTransport) -> _SpecRecorder:
    assert isinstance(transport, _SpecRecorder)
    return transport


def _access_token(transport: MailTransport) -> str | None:
    spec = _recorder(transport).spec
    assert spec is not None
    return spec.access_token


type _Wired = tuple[MailService, Engine, Vault]


@pytest.fixture
async def wired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Wired:
    """A service whose Gmail provider builds recorders instead of real adapters."""
    _SpecRecorder.instances = []
    monkeypatch.setattr(mail_service, "_TRANSPORTS", {PROVIDER_GMAIL: _SpecRecorder})
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(tmp_path / "keyfile.json")
    await vault.setup("pw")
    credentials = CredentialStore(engine, vault)
    _ = await credentials.set_key("operator", "google_oauth", "client-secret")
    return MailService(engine, vault, credentials, _NoModels()), engine, vault


async def _oauth_account(
    service: MailService, engine: Engine, vault: Vault, bundle: TokenBundle
) -> MailAccount:
    account = MailAccount(
        owner_id="operator",
        name="Personal",
        address_enc=vault.encrypt_str("operator@example.com"),
        provider=PROVIDER_GMAIL,
        auth_kind=AUTH_OAUTH,
        config={"oauth_provider": "google", "oauth_client_id": "client-id"},
        secret_enc=service.secrets.seal_bundle(bundle),
    )

    def work(session: Session) -> None:
        session.add(account)

    await in_session(engine, work)
    return account


async def test_a_live_token_reuses_the_cached_transport(
    wired: _Wired, monkeypatch: pytest.MonkeyPatch
):
    """The cache still has to be a cache — one connection per account, not one per call."""
    service, engine, vault = wired
    account = await _oauth_account(service, engine, vault, LIVE_BUNDLE)

    async def _never(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("a live token must not trigger a refresh")

    monkeypatch.setattr("services.mail.oauth.refresh_tokens", _never)
    first = await service.transport(account)
    assert await service.transport(account) is first
    assert len(_SpecRecorder.instances) == 1


async def test_every_call_resolves_credentials_before_consulting_the_cache(
    wired: _Wired, monkeypatch: pytest.MonkeyPatch
):
    """The load-bearing half, stated as its own rule: `open_access` runs on *every* call,
    not just a miss. It is the only code that notices an access token is near expiry and
    the only code that refreshes one, so a cache checked ahead of it pins the account to
    whatever token it happened to hold at build time. A cache hit is decided *after*.
    """
    service, engine, vault = wired
    account = await _oauth_account(service, engine, vault, LIVE_BUNDLE)
    opened = 0
    original = service.secrets.open_access

    async def counting(row: MailAccount) -> tuple[str | None, str | None]:
        nonlocal opened
        opened += 1
        return await original(row)

    monkeypatch.setattr(service.secrets, "open_access", counting)
    first = await service.transport(account)
    second = await service.transport(account)

    assert second is first  # still one connection...
    assert opened == 2  # ...but the credentials behind it were re-checked


async def test_a_rotated_token_rebuilds_the_transport(
    wired: _Wired, monkeypatch: pytest.MonkeyPatch
):
    """The bug this guards: the adapter's token is frozen at construction, so a cache hit
    that skips `open_access` pins the account to a token that lapses within the hour and
    then fails every operation until the operator edits or probes the account."""
    service, engine, vault = wired
    account = await _oauth_account(
        service,
        engine,
        vault,
        TokenBundle(access_token="stale", refresh_token="rt-1", expires_at=EXPIRED),
    )

    async def _refresh(_provider: OAuthProvider, **_credentials: str) -> TokenBundle:
        return TokenBundle(access_token="fresh", refresh_token="rt-2", expires_at=LIVE)

    monkeypatch.setattr("services.mail.oauth.refresh_tokens", _refresh)
    stale = await service.transport(account)
    assert _access_token(stale) == "fresh"  # the very first build already refreshed

    # Age the freshly-sealed bundle so the next call has to refresh again.
    account.secret_enc = service.secrets.seal_bundle(
        TokenBundle(access_token="fresh", refresh_token="rt-2", expires_at=EXPIRED)
    )

    async def _refresh_again(_provider: OAuthProvider, **_credentials: str) -> TokenBundle:
        return TokenBundle(access_token="fresher", refresh_token="rt-3", expires_at=LIVE)

    monkeypatch.setattr("services.mail.oauth.refresh_tokens", _refresh_again)
    rebuilt = await service.transport(account)

    assert rebuilt is not stale
    assert _access_token(rebuilt) == "fresher"
    assert _recorder(stale).closed is True  # the old connection is released, not leaked


async def test_an_installed_transport_is_cached_on_the_same_terms_as_a_built_one(wired: _Wired):
    """Tests wire fakes in to keep the network out; `install_transport` records the same
    credentials the service would have, so an installed adapter is subject to the same
    rule rather than being an exception to it — nothing lands in the cache unaccounted
    for."""
    service, engine, vault = wired
    account = await _oauth_account(service, engine, vault, LIVE_BUNDLE)
    fake = _SpecRecorder(spec=None)
    await install_transport(service, "operator", account.id, fake)

    assert await service.transport(account) is fake
    assert service.transports[account.id].credentials == (None, "live")


async def test_dropping_a_transport_forgets_its_credentials(wired: _Wired):
    service, engine, vault = wired
    account = await _oauth_account(service, engine, vault, LIVE_BUNDLE)
    first = await service.transport(account)
    await service.drop_transport(account.id)

    assert _recorder(first).closed is True
    assert account.id not in service.transports
    assert await service.transport(account) is not first
