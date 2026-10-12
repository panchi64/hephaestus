"""The platform VM: where agent commands, previews and the web services run."""

from __future__ import annotations

from .driver import ExecResult, VMDriver, VMError
from .egress import DENIED_MARKER, EgressGateway
from .platform import GuestLayout, PlatformVM
from .tunnel import PreviewPort
from .workspace import GuestWorkspace

__all__ = [
    "DENIED_MARKER",
    "EgressGateway",
    "ExecResult",
    "GuestLayout",
    "GuestWorkspace",
    "PlatformVM",
    "PreviewPort",
    "VMDriver",
    "VMError",
]
