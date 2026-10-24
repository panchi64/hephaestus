"""The VMs — their state, and the operator's control to restart them.

Resetting boots a fresh services machine and deletes every conversation's machine, each
of which boots again on its next command. Nothing durable lives in a machine: the files a
conversation works on are host folders it only mounts.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from routes import deps
from services.vm import ServicesMachine

router = APIRouter(prefix="/vm", tags=["vm"])


class VMStatus(BaseModel):
    state: Literal["ready", "starting", "unavailable"]
    #: Why it is unavailable, or what it is doing while it starts.
    detail: str | None = None


def _vm(request: Request) -> ServicesMachine:
    vm = deps.vm(request)
    if vm is None:
        raise HTTPException(status_code=404, detail="the VM is not configured")
    return vm


def _status(vm: ServicesMachine) -> VMStatus:
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
    machines = deps.machines(request)
    if machines is not None:
        await machines.release_all()
    await vm.reset()
    return _status(vm)
