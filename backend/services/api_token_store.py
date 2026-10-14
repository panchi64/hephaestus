"""Inbound scoped API tokens (`AUTH-4`) — issue, list, revoke, verify.

The store behind the Access Tokens page and the auth gate's second authentication method.
Deliberately **not** the outbound key store (`services/credential_store.py`): that holds the
keys this system calls other services with; this issues credentials other clients call *us*
with.

A minted token is ``odyt_<prefix>_<secret>``. Only the prefix and a one-way Argon2id hash of
the secret are persisted, so the plaintext exists exactly once — in the issue response — and
is unrecoverable afterwards (`XC-SEC-3`). Verification is therefore one indexed lookup on the
public prefix plus one constant-time hash comparison; nothing about the secret is learnable
from how long a rejection takes.

Argon2id is the right hash at rest but far too costly to pay on *every* request a token
authenticates, so a successful verification is remembered in memory — keyed by a digest of
the presented token, never the token — exactly like the operator's session tokens live in
memory in ``core.auth``. Revoking drops the token's cache entry, so revocation takes effect
on the next request rather than at the next restart.

Raises domain errors only (`NotFoundError`, `ValueError` on an unknown scope); the route maps
to HTTP.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import Engine
from sqlmodel import Session, col, select

from core.api_scopes import ScopeTable
from core.auth import TokenIdentity
from core.crypto import hash_password, verify_password
from core.db import in_session
from core.exceptions import NotFoundError
from models.api_token import ApiToken

# `odyt_` marks a string as *our* inbound token, so the gate can tell an API token apart
# from an operator session token before touching the database — a session never pays for a
# token lookup, and a malformed credential never pays for an Argon2 verification.
TOKEN_SCHEME = "odyt_"

_PREFIX_BYTES = 4  # 8 hex chars — public, indexed, shown in listings
_SECRET_BYTES = 32  # 256 bits of entropy in the secret half

# How long a `last_used_at` write is suppressed after the previous one. The column exists so
# the operator can spot a stale or unexpectedly-active token, which needs minute resolution,
# not a database write on every authenticated request.
_TOUCH_INTERVAL_S = 60.0


@dataclass(frozen=True)
class ApiTokenInfo:
    """A token as the operator sees it — never the secret."""

    id: str
    label: str
    prefix: str
    scopes: list[str]
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


@dataclass(frozen=True)
class IssuedToken:
    """The one and only time the plaintext token is available."""

    token: str
    info: ApiTokenInfo


def _info(row: ApiToken) -> ApiTokenInfo:
    return ApiTokenInfo(
        id=row.id,
        label=row.label,
        prefix=row.token_prefix,
        scopes=list(row.scopes),
        created_at=row.created_at,
        last_used_at=row.last_used_at,
        revoked_at=row.revoked_at,
    )


def _split(presented: str) -> tuple[str, str] | None:
    """``odyt_<prefix>_<secret>`` → ``(prefix, secret)``, or ``None`` if malformed."""
    if not presented.startswith(TOKEN_SCHEME):
        return None
    prefix, _, secret = presented[len(TOKEN_SCHEME) :].partition("_")
    if not prefix or not secret:
        return None
    return prefix, secret


def _digest(presented: str) -> str:
    """A cache key derived from the presented token — the token itself is never a key."""
    return hashlib.sha256(presented.encode()).hexdigest()


class ApiTokenStore:
    def __init__(self, engine: Engine, scope_table: ScopeTable) -> None:
        self._engine = engine
        # The same table the auth gate resolves against, so a scope this store lets
        # onto a token is exactly a scope the gate can honor.
        self._scope_table = scope_table
        # digest(presented token) → identity, for tokens already verified this process.
        self._verified: dict[str, TokenIdentity] = {}
        # token id → monotonic time of its last `last_used_at` write.
        self._touched: dict[str, float] = {}

    # --- operator-facing -----------------------------------------------------------

    async def issue(self, owner_id: str, label: str, scopes: list[str]) -> IssuedToken:
        """Mint a token, persist only its hash, and return the plaintext once."""
        label = label.strip()
        if not label:
            raise ValueError("a token needs a label")
        unknown = self._scope_table.unknown_scopes(scopes)
        if unknown:
            raise ValueError(f"unknown scope(s): {', '.join(unknown)}")
        if not scopes:
            raise ValueError("a token needs at least one scope")

        prefix = secrets.token_hex(_PREFIX_BYTES)
        secret = secrets.token_urlsafe(_SECRET_BYTES)
        plaintext = f"{TOKEN_SCHEME}{prefix}_{secret}"
        token_hash = await asyncio.to_thread(hash_password, secret)

        row = ApiToken(
            owner_id=owner_id,
            label=label,
            token_prefix=prefix,
            token_hash=token_hash,
            # Deduplicated but order-preserving, so the listing reads back as chosen.
            scopes=list(dict.fromkeys(scopes)),
        )

        def work(session: Session) -> ApiToken:
            session.add(row)
            session.flush()
            return row

        stored = await in_session(self._engine, work)
        return IssuedToken(token=plaintext, info=_info(stored))

    async def list(self, owner_id: str) -> list[ApiTokenInfo]:
        def work(session: Session) -> list[ApiTokenInfo]:
            rows = session.exec(
                select(ApiToken)
                .where(ApiToken.owner_id == owner_id)
                .order_by(col(ApiToken.created_at).desc())
            ).all()
            return [_info(row) for row in rows]

        return await in_session(self._engine, work)

    async def revoke(self, owner_id: str, token_id: str) -> ApiTokenInfo:
        """Tombstone a token. Idempotent: revoking twice keeps the first timestamp."""

        def work(session: Session) -> ApiToken:
            row = session.exec(
                select(ApiToken).where(ApiToken.owner_id == owner_id, ApiToken.id == token_id)
            ).first()
            if row is None:
                raise NotFoundError(f"unknown token {token_id!r}")
            if row.revoked_at is None:
                row.revoked_at = datetime.now(UTC)
                session.add(row)
                session.flush()
            return row

        row = await in_session(self._engine, work)
        # Drop any cached verification so the next request re-reads the tombstone.
        for digest, identity in list(self._verified.items()):
            if identity.token_id == token_id:
                del self._verified[digest]
        self._touched.pop(token_id, None)
        return _info(row)

    # --- the auth gate -------------------------------------------------------------

    def cached(self, presented: str) -> TokenIdentity | None:
        """A previously-verified token's identity, without touching the database.

        The gate consults this first so an established client never re-pays Argon2id —
        and so brute force, which by definition never hits the cache, always falls through
        to the rate limiter."""
        return self._verified.get(_digest(presented))

    async def authenticate(self, presented: str) -> TokenIdentity | None:
        """Verify a presented token against the store. ``None`` = not a valid token.

        Rejects revoked tokens, and compares in constant time (Argon2's own verify), so a
        near-miss is indistinguishable from a wild guess."""
        parts = _split(presented)
        if parts is None:
            return None
        prefix, secret = parts

        def work(session: Session) -> ApiToken | None:
            return session.exec(select(ApiToken).where(ApiToken.token_prefix == prefix)).first()

        row = await in_session(self._engine, work)
        if row is None or row.revoked_at is not None:
            return None
        if not await asyncio.to_thread(verify_password, row.token_hash, secret):
            return None

        identity = TokenIdentity(token_id=row.id, owner_id=row.owner_id, scopes=tuple(row.scopes))
        self._verified[_digest(presented)] = identity
        return identity

    async def touch(self, token_id: str) -> None:
        """Record that the token authenticated, at most once per interval."""
        now = time.monotonic()
        last = self._touched.get(token_id)
        if last is not None and now - last < _TOUCH_INTERVAL_S:
            return
        self._touched[token_id] = now

        def work(session: Session) -> None:
            row = session.get(ApiToken, token_id)
            if row is not None:
                row.last_used_at = datetime.now(UTC)
                session.add(row)

        await in_session(self._engine, work)
