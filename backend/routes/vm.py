"""The platform VM — its state, and the operator's control to rebuild it.

Resetting boots a fresh VM from the clean image. Nothing durable lives in the guest, so
nothing is lost: every workspace is brought back in on its next command, and the web
services start again on their own.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from services.vm import PlatformVM

router = APIRouter(prefix="/vm", tags=["vm"])


class VMStatus(BaseModel):
    state: Literal["ready", "starting", "unavailable"]
    #: Why it is unavailable, or what it is doing while it starts.
    detail: str | None = None


def _vm(request: Request) -> PlatformVM:
    vm = getattr(request.app.state, "vm", None)
    if vm is None:
        raise HTTPException(status_code=404, detail="the platform VM is not configured")
    return vm


def _status(vm: PlatformVM) -> VMStatus:
    if vm.available:
        return VMStatus(state="ready")
    if vm.failure is not None:
        return VMStatus(state="unavailable", detail=vm.failure)
    return VMStatus(state="starting", detail=vm.progress)


@router.get("", response_model=VMStatus)
async def get_vm(request: Request) -> VMStatus:
    return _status(_vm(request))


@router.post("/reset", response_model=VMStatus)
async def reset_vm(request: Request) -> VMStatus:
    vm = _vm(request)
    await vm.reset()
    return _status(vm)
