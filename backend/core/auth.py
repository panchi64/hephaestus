"""Operator authentication and the global auth gate.

Single operator: the password both verifies login and derives the encryption key
(through the vault). :class:`AuthManager` tracks issued session tokens; they live
in memory and are cleared on lock or restart — which is also when the vault
re-locks, so a valid token always implies an unlocked vault.

Alongside that session there is a second, *programmatic* way in: a scoped API token
(`AUTH-4`), issued to a client and presented as a bearer credential. The two are told apart
by the token's own shape (``odyt_``), and they differ in reach — a session is the operator
and may do anything, while a token may only touch the surfaces its scopes claim
(``core.api_scopes``, deny-by-default). Both are throttled through one shared attempt
limiter (`AUTH-1`), keyed per client, so guessing either credential is rate-bound.

:class:`AuthMiddleware` is a **pure ASGI** middleware (not BaseHTTPMiddleware) so
it never buffers responses — important for the SSE event stream. It enforces the
gate before any feature is reached and passes streaming responses through
untouched.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from typing import Protocol, cast

from starlette.applications import Starlette
from starlette.datastructures import State
from starlette.types import ASGIApp, Receive, Scope, Send

from core.api_scopes import ScopeTable
from core.exceptions import RateLimitedError
from core.ratelimit import RateLimiter

SESSION_COOKIE = "hephaestus_session"

# Marks a credential as an inbound API token rather than an operator session token, so the
# gate can route it without a database lookup. The minted form is `odyt_<prefix>_<secret>`.
TOKEN_SCHEME = "odyt_"

# The request-state key the gate sets on a request it let in on an API token.
_VIA_API_TOKEN = "via_api_token"


def via_api_token(state: State) -> bool:
    """Whether the request owning ``state`` came in on an API token, not the operator's session."""
    return bool(getattr(state, _VIA_API_TOKEN, False))

# Password guesses and token guesses are the same attack, so they share one limiter under
# separate key namespaces. Generous enough that a human never meets it and a script does.
_AUTH_ATTEMPT_RATE_PER_MINUTE = 12.0
_AUTH_ATTEMPT_BURST = 10

# Reachable without authentication: status probe, first-run setup, login, and
# liveness. Everything else — including the *state-changing* /auth/lock and
# /auth/logout — stays behind the gate, so an unauthenticated caller can't lock
# the vault or revoke sessions.
#
# `/setup/reset` is here for the same reason `/setup` is: it answers the operator who
# has no way to authenticate yet. It is destructive, so it does not rely on that alone
# — the route refuses unless the workspace database is provably gone, a state a live
# workspace cannot be in. See `routes/auth.py`.
_PUBLIC_PATHS = frozenset(
    {"/auth/status", "/auth/login", "/setup", "/setup/reset", "/openapi.json"}
)
# Prefixes whose sub-paths are also public (liveness probes, the docs UIs) — the
# core set. A feature whose unguessable path token *is* the credential (the preview
# proxy's `/previews`, the task webhook's `/tasks/hooks`) declares its own prefix on
# its manifest (`public_prefixes`); the assembly passes the combined tuple in, so
# claiming an auth exemption stays a deliberate, reviewable act on the feature.
_PUBLIC_PREFIXES = ("/health", "/docs", "/redoc")


@dataclass(frozen=True)
class TokenIdentity:
    """Who a verified API token is, and how far it reaches."""

    token_id: str
    owner_id: str
    scopes: tuple[str, ...]


class ApiTokenAuthenticator(Protocol):
    """What the gate needs from the token store — the whole of the dependency."""

    def cached(self, presented: str) -> TokenIdentity | None: ...

    async def authenticate(self, presented: str) -> TokenIdentity | None: ...

    async def touch(self, token_id: str) -> None: ...


class _GateSettings(Protocol):
    @property
    def auth_enabled(self) -> bool: ...


class _GateVault(Protocol):
    @property
    def is_unlocked(self) -> bool: ...


class _GateState(Protocol):
    """The slice of ``app.state`` the gate reads."""

    @property
    def settings(self) -> _GateSettings: ...

    @property
    def auth_manager(self) -> AuthManager: ...

    @property
    def vault(self) -> _GateVault: ...


class _GateApp(Protocol):
    @property
    def state(self) -> _GateState: ...


def _scope_str(scope: Scope, key: str) -> str:
    value = cast("object", scope.get(key))
    return value if isinstance(value, str) else ""


def api_token_store(state: State) -> ApiTokenAuthenticator | None:
    """The shared token store the app wired at startup, or ``None`` when there is none.

    `core` must not import `services`, so the gate never *builds* the store — it only
    knows the protocol above and reads whatever the app hung on ``app.state``. ``None``
    (a test app assembled without the lifespan) simply means there is no second
    authentication method and only the operator session is accepted.
    """
    store: ApiTokenAuthenticator | None = getattr(state, "api_tokens", None)
    return store


def auth_attempt_limiter(state: State) -> RateLimiter:
    """The shared login/token attempt throttle, built on first use."""
    limiter: RateLimiter | None = getattr(state, "auth_attempt_limiter", None)
    if limiter is None:
        limiter = RateLimiter(
            rate_per_second=_AUTH_ATTEMPT_RATE_PER_MINUTE / 60.0,
            burst=_AUTH_ATTEMPT_BURST,
        )
        state.auth_attempt_limiter = limiter
    return limiter


class AuthManager:
    def __init__(self) -> None:
        self._tokens: set[str] = set()

    def issue(self) -> str:
        token = secrets.token_urlsafe(32)
        self._tokens.add(token)
        return token

    def verify(self, token: str | None) -> bool:
        return token is not None and token in self._tokens

    def revoke(self, token: str) -> None:
        self._tokens.discard(token)

    def revoke_all(self) -> None:
        self._tokens.clear()


def _under(path: str, prefixes: tuple[str, ...]) -> bool:
    return any(path == p or path.startswith(p + "/") for p in prefixes)


def _is_core_public(path: str) -> bool:
    """Reachable with no credential **and** with the vault locked — the surfaces whose
    whole job is to get the operator past the lock (status, login, first-run setup) plus
    liveness and the docs UIs. Nothing here touches operator data."""
    return path in _PUBLIC_PATHS or _under(path, _PUBLIC_PREFIXES)


def _is_feature_public(path: str, extra_prefixes: tuple[str, ...]) -> bool:
    """A feature's own auth exemption (the preview proxy, the task webhook): the
    unguessable path token *is* the credential, so no session is required — but these
    surfaces do reach operator data, so they stay behind the vault-locked gate like
    every other feature. See :class:`AuthMiddleware`."""
    return _under(path, extra_prefixes)


def token_from_headers(authorization: str | None, cookie_token: str | None) -> str | None:
    """Resolve the session token from a bearer header or the session cookie."""
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return cookie_token


def _token_from_scope(scope: Scope) -> str | None:
    # HTTP header bytes are latin-1 on the wire, not UTF-8: decoding them as UTF-8 would
    # raise on any high byte a client happens to send (a legacy user-agent, a filename in
    # a custom header) and 500 the request from inside the middleware, before any handler.
    raw_headers = cast("list[tuple[bytes, bytes]]", scope.get("headers", []))
    headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in raw_headers}
    cookie_token = None
    for part in headers.get("cookie", "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == SESSION_COOKIE:
            cookie_token = value
            break
    return token_from_headers(headers.get("authorization"), cookie_token)


def client_key(client: tuple[str, int] | None) -> str:
    """The rate-limit key for a caller. Brute force varies the credential, not the
    source, so the source is what has to be throttled."""
    return client[0] if client else "unknown"


async def _reject(
    send: Send, status: int, detail: str, headers: list[tuple[bytes, bytes]] | None = None
) -> None:
    body = json.dumps({"detail": detail}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json"), *(headers or [])],
        }
    )
    await send({"type": "http.response.body", "body": body})


# What a failed API-token authentication should become on the wire.
_Rejection = tuple[int, str, list[tuple[bytes, bytes]]]
_UNAUTHENTICATED: _Rejection = (401, "authentication required", [])


async def _authenticate_api_token(
    state: State, scope: Scope, presented: str | None, scope_table: ScopeTable
) -> _Rejection | None:
    """Try the presented credential as a scoped API token. ``None`` = authenticated.

    Order matters: the in-memory cache is consulted before the limiter so a working client
    is never throttled, while a guess — which can't be cached — always is, and only then
    pays for the Argon2id comparison."""
    if presented is None or not presented.startswith(TOKEN_SCHEME):
        return _UNAUTHENTICATED
    store = api_token_store(state)
    if store is None:
        return _UNAUTHENTICATED

    identity = store.cached(presented)
    if identity is None:
        try:
            auth_attempt_limiter(state).check(f"token:{client_key(scope.get('client'))}")
        except RateLimitedError as exc:
            retry_after = str(max(1, int(exc.retry_after_s))).encode()
            return (429, str(exc), [(b"retry-after", retry_after)])
        identity = await store.authenticate(presented)
        if identity is None:
            return _UNAUTHENTICATED

    required = scope_table.scope_for_path(_scope_str(scope, "path"), _scope_str(scope, "method"))
    if required is None or required not in identity.scopes:
        # Deny-by-default: an unclaimed path (`/tokens`, `/vault`, `/shell`) is out of
        # reach for every token, whatever it carries.
        return (403, "this token's scopes do not cover this endpoint", [])

    await store.touch(identity.token_id)
    return None


class AuthMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        scope_table: ScopeTable,
        extra_public_prefixes: tuple[str, ...] = (),
    ) -> None:
        self.app: ASGIApp = app
        self._scope_table: ScopeTable = scope_table
        self._extra_public_prefixes: tuple[str, ...] = extra_public_prefixes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = _scope_str(scope, "path")
        if (
            scope["type"] != "http"
            or _scope_str(scope, "method") == "OPTIONS"
            or _is_core_public(path)
        ):
            return await self.app(scope, receive, send)

        app_state = cast("Starlette", scope["app"]).state
        state = cast("_GateApp", scope["app"]).state
        # A feature's declared exemption waives the *session*, not the lock: the preview
        # proxy and the task webhook authenticate by unguessable path token, but both
        # reach operator data, so locking must take them offline with everything else.
        # Without this they kept serving the agent's live workspace after `/auth/lock`.
        if state.settings.auth_enabled and not _is_feature_public(
            path, self._extra_public_prefixes
        ):
            presented = _token_from_scope(scope)
            # The operator session comes first: an in-memory set membership test, so the
            # browser's every request stays free of the token path entirely.
            if not state.auth_manager.verify(presented):
                rejection = await _authenticate_api_token(
                    app_state, scope, presented, self._scope_table
                )
                if rejection is not None:
                    return await _reject(send, *rejection)
                cast("dict[str, object]", scope.setdefault("state", {}))[_VIA_API_TOKEN] = True
        if not state.vault.is_unlocked:
            return await _reject(send, 423, "the vault is locked")
        await self.app(scope, receive, send)
