"""The Private area's PIN and lock. Operator-session only: no token scope claims it."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from core.auth import auth_attempt_limiter, client_key
from core.exceptions import PermissionDeniedError
from routes import deps
from routes.deps import OPERATOR_ID
from services.private_area import PrivateStatus

router = APIRouter(
    prefix="/private", tags=["private"], dependencies=[Depends(deps.require_operator)]
)


class PrivateStatusOut(BaseModel):
    pin_set: bool
    unlocked: bool
    relock_at: datetime | None
    relock_minutes: int


class PinSet(BaseModel):
    pin: str
    #: Proof for replacing a PIN already set: the current PIN or the account password.
    current_pin: str | None = None
    password: str | None = None


class PinUnlock(BaseModel):
    pin: str


class RelockUpdate(BaseModel):
    minutes: int


def _out(status: PrivateStatus) -> PrivateStatusOut:
    return PrivateStatusOut(
        pin_set=status.pin_set,
        unlocked=status.unlocked,
        relock_at=status.relock_at,
        relock_minutes=status.relock_minutes,
    )


def _throttle(request: Request) -> None:
    """PIN guesses share the login throttle: both are guesses at an operator secret."""
    limiter = auth_attempt_limiter(deps.app_state(request))
    limiter.check(f"private:{client_key(request.scope.get('client'))}")


@router.get("/status", response_model=PrivateStatusOut)
async def private_status(request: Request) -> PrivateStatusOut:
    return _out(await deps.private_area(request).status(OPERATOR_ID))


@router.post("/pin", response_model=PrivateStatusOut)
async def set_pin(body: PinSet, request: Request) -> PrivateStatusOut:
    """Set the PIN, or replace it with the current PIN or the account password. Opens the
    area."""
    _throttle(request)
    area = deps.private_area(request)
    await area.set_pin(OPERATOR_ID, body.pin, current_pin=body.current_pin, password=body.password)
    return _out(await area.status(OPERATOR_ID))


@router.post("/unlock", response_model=PrivateStatusOut)
async def unlock(body: PinUnlock, request: Request) -> PrivateStatusOut:
    """Open the area. The fifth wrong PIN in a row locks the vault and signs out."""
    _throttle(request)
    area = deps.private_area(request)
    if not await area.unlock(OPERATOR_ID, body.pin):
        # Not 401: the client reads any 401 as a dead session and signs the operator out.
        raise PermissionDeniedError("wrong PIN")
    return _out(await area.status(OPERATOR_ID))


@router.post("/lock", response_model=PrivateStatusOut)
async def lock(request: Request) -> PrivateStatusOut:
    area = deps.private_area(request)
    area.lock()
    return _out(await area.status(OPERATOR_ID))


@router.put("/relock", response_model=PrivateStatusOut)
async def set_relock(body: RelockUpdate, request: Request) -> PrivateStatusOut:
    """How many idle minutes the open area stays open."""
    area = deps.private_area(request)
    _ = await area.set_relock_minutes(OPERATOR_ID, body.minutes)
    return _out(await area.status(OPERATOR_ID))
