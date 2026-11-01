"""Auth surface: status, first-run setup, workspace reset, login, logout, lock.

Login and setup unlock the vault (deriving the encryption key from the password)
and issue a session token — returned in the body (for bearer clients) and set as
an httpOnly cookie (for the browser, including the SSE stream).
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from core.auth import SESSION_COOKIE, auth_attempt_limiter, client_key, token_from_headers
from core.exceptions import RateLimitedError
from core.workspace import WorkspaceOpenError
from routes import deps
from routes.deps import OPERATOR_ID
from services import onboarding
from services.workspace_reset import reset_workspace

router = APIRouter(tags=["auth"])

_MIN_PASSWORD_LEN = 8


class PasswordBody(BaseModel):
    password: str


class AuthStatus(BaseModel):
    initialized: bool
    unlocked: bool
    auth_enabled: bool
    #: The keyfile is here but the database it belonged to is not — an operator who
    #: cleared `app.db` to start over, met by an unlock prompt for a key that now
    #: protects an empty workspace. Only meaningful while `initialized`.
    db_missing: bool
    #: `closed`, `opening`, `open` or `failed`: whether the decrypted workspace is up.
    workspace: str
    #: Why the last open failed (no volume backend, a key that doesn't open the database).
    workspace_failure: str | None


class ResetSummaryOut(BaseModel):
    """What `POST /setup/reset` actually removed. Reported rather than assumed: the
    client tells the operator what left their disk, not what we hoped would."""

    removed: list[str]
    bytes_freed: int
    failed: list[str]


class TokenResponse(BaseModel):
    token: str


def _issue_session(request: Request, response: Response) -> TokenResponse:
    token = deps.auth_manager(request).issue()
    # secure=False: the app serves plain HTTP; put TLS in front for remote use.
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", secure=False)
    return TokenResponse(token=token)


async def _open_workspace(request: Request) -> None:
    """Open the workspace under the key just unlocked; on failure re-lock and answer 503."""
    try:
        await deps.workspace(request).open()
    except WorkspaceOpenError as exc:
        deps.vault(request).lock()
        raise HTTPException(
            status_code=503, detail=f"the workspace could not be opened: {exc}"
        ) from None


@router.get("/auth/status", response_model=AuthStatus)
async def auth_status(request: Request) -> AuthStatus:
    vault = deps.vault(request)
    workspace = deps.workspace(request)
    return AuthStatus(
        initialized=vault.is_initialized,
        unlocked=vault.is_unlocked,
        auth_enabled=deps.settings(request).auth_enabled,
        db_missing=vault.is_initialized and not deps.workspace_db_intact(request),
        workspace=workspace.status.value,
        workspace_failure=workspace.failure,
    )


@router.post("/setup", response_model=TokenResponse)
async def setup(body: PasswordBody, request: Request, response: Response) -> TokenResponse:
    """First run only: choose the operator password (which derives the key)."""
    vault = deps.vault(request)
    if vault.is_initialized:
        raise HTTPException(status_code=409, detail="already set up")
    if len(body.password) < _MIN_PASSWORD_LEN:
        raise HTTPException(status_code=422, detail="password must be at least 8 characters")
    await vault.setup(body.password)
    await _open_workspace(request)
    # The workspace this just created lives in *this* database, so the keyfile and the
    # database are in step again — without this, a setup performed after a reset would
    # keep reporting `db_missing` for the rest of the process's life.
    deps.mark_workspace_db_intact(request)
    await onboarding.mark_pending(deps.settings_store(request), OPERATOR_ID)
    return _issue_session(request, response)


@router.post("/setup/reset", response_model=ResetSummaryOut)
async def reset(request: Request) -> ResetSummaryOut:
    """Abandon a workspace whose database is gone: remove the key and everything sealed
    under it, so the next `/auth/status` reports first-run and setup can be offered.

    **The guard is state, not credentials** — the same posture `/setup` already has, and
    for the same reason: there is no one to authenticate to yet. What makes that safe is
    that the state it demands cannot describe a live workspace. A workspace in use has a
    database that predates this boot, so this endpoint can never reach one; and if the
    vault is unlocked, whoever is asking is already inside and has ordinary ways to delete
    their data.
    """
    vault = deps.vault(request)
    if not vault.is_initialized:
        raise HTTPException(status_code=409, detail="nothing to reset — not set up")
    if vault.is_unlocked:
        raise HTTPException(status_code=409, detail="the workspace is unlocked")
    if deps.workspace_db_intact(request):
        raise HTTPException(status_code=409, detail="the workspace database is intact")
    summary = await asyncio.to_thread(reset_workspace, deps.settings(request).data_dir)
    return ResetSummaryOut(
        removed=summary.removed,
        bytes_freed=summary.bytes_freed,
        failed=summary.failed,
    )


@router.post("/auth/login", response_model=TokenResponse)
async def login(body: PasswordBody, request: Request, response: Response) -> TokenResponse:
    vault = deps.vault(request)
    if not vault.is_initialized:
        raise HTTPException(status_code=409, detail="not set up yet")
    # Password guessing is rate-bound per caller (`AUTH-1`), through the same limiter the
    # gate throttles API-token guesses with — one attack, one throttle.
    limiter = auth_attempt_limiter(deps.app_state(request))
    key = f"login:{client_key(request.scope.get('client'))}"
    try:
        limiter.check(key)
    except RateLimitedError as exc:
        raise HTTPException(
            status_code=429,
            detail=str(exc),
            headers={"Retry-After": str(max(1, int(exc.retry_after_s)))},
        ) from None
    if not await vault.unlock(body.password):
        raise HTTPException(status_code=401, detail="invalid password")
    await _open_workspace(request)
    return _issue_session(request, response)


@router.post("/auth/logout")
async def logout(request: Request, response: Response) -> dict[str, str]:
    """End the session and lock: logging out closes the workspace and zeroes the key.

    The DEK is process-global for the single operator, so "log out" that kept it alive
    would leave every decrypted secret, PTY and mounted file running. Both endpoints
    converge on the same teardown; `/auth/lock` keeps no cookie semantics of its own.
    """
    token = token_from_headers(
        request.headers.get("authorization"), request.cookies.get(SESSION_COOKIE)
    )
    if token:
        deps.auth_manager(request).revoke(token)
    await deps.workspace(request).lock()
    deps.auth_manager(request).revoke_all()
    response.delete_cookie(SESSION_COOKIE)
    return {"status": "logged out"}


@router.post("/auth/lock")
async def lock(request: Request) -> dict[str, str]:
    """Close the workspace, wipe the key and revoke all sessions; re-unlock requires login."""
    await deps.workspace(request).lock()
    deps.auth_manager(request).revoke_all()
    return {"status": "locked"}
