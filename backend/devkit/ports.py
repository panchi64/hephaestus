"""Which ports a dev instance gets, and why it gets the same ones tomorrow.

Ports are allocated in **slots** rather than one at a time: a slot is a whole triple
(backend, frontend, stub) at the same offset, so an instance's three services are always
a fixed distance apart and one number identifies the lot. That is worth more than the
packing it wastes — the number appears in a URL the operator reads, in a CORS origin, in
a generated launch config, and in whatever a future session is about to be told, and a
triple that could drift apart is three things to keep in step instead of one.

The bases sit clear of 8000 and 5173, which is where the operator's own instance lives.
That separation is structural rather than checked-for: no slot can reach those numbers,
so there is no arithmetic to get wrong and no conflict to detect.

Allocation happens **once**, when an instance is first created, and is then recorded.
A later run reads the recorded slot back rather than re-deriving it, because by then the
instance's own services are usually listening on those ports and a fresh probe would
read its own backend as a conflict and move it — renaming URLs out from under whoever
was using them.
"""

from __future__ import annotations

import errno
import socket

#: The first port of each service's range. Chosen well above the operator's 8000/5173.
BACKEND_BASE = 8200
FRONTEND_BASE = 5273
STUB_BASE = 8300

#: How many parallel dev instances one host will hand out slots for. A ceiling rather
#: than a limit anyone should reach: it exists so a bug that fails to find a free slot
#: says so instead of scanning ports forever.
MAX_SLOTS = 20


class NoFreeSlot(RuntimeError):
    """Every slot is taken. Carries the remedy, because the caller prints it verbatim."""


def slot_ports(slot: int) -> tuple[int, int, int]:
    """The (backend, frontend, stub) triple for a slot."""
    return BACKEND_BASE + slot, FRONTEND_BASE + slot, STUB_BASE + slot


#: The loopback addresses a local server can be reached on. Both are checked everywhere
#: a port is probed, because which one a server picks is the runtime's choice and not
#: ours: uvicorn and vite are each told ``127.0.0.1`` explicitly, but a server left to
#: the name ``localhost`` takes whatever it resolves to first — ``::1`` on a stock macOS.
LOOPBACKS: tuple[tuple[int, str], ...] = (
    (socket.AF_INET, "127.0.0.1"),
    (socket.AF_INET6, "::1"),
)


def _can_bind(family: int, host: str, port: int) -> bool:
    """Whether this port is free on one loopback address. A family the host has no
    stack for is not an occupied port — it answers free, so a v4-only machine is not
    told every port is taken."""
    try:
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            probe.bind((host, port))
    except OSError as exc:
        return exc.errno in (errno.EAFNOSUPPORT, errno.EADDRNOTAVAIL)
    return True


def is_free(port: int) -> bool:
    """Whether nothing is listening on this loopback port.

    Deliberately without ``SO_REUSEADDR``: the question is "can a server take this
    port", not "can I momentarily bind it", and the reuse flag would answer yes over a
    socket another process is still holding.

    **Both loopback families, because a server may be on either.** Vite binds
    ``localhost``, which on this host resolves to ``::1`` — so an IPv4-only bind test
    succeeds over a running dev server and reports its port free. That reads as "the
    frontend is down" in ``status`` and as a free slot in ``allocate``, which is how a
    second instance ends up handed a port the first is already serving on.
    """
    return all(_can_bind(family, host, port) for family, host in LOOPBACKS)


def allocate(taken: set[int]) -> int:
    """The lowest slot that neither another instance has claimed nor the host is using.

    ``taken`` is what the other instances on this host have recorded — checked first and
    on its own, because a slot belonging to an instance that simply is not running right
    now would otherwise look free and be handed out twice.
    """
    for slot in range(MAX_SLOTS):
        if slot in taken:
            continue
        if all(is_free(port) for port in slot_ports(slot)):
            return slot
    raise NoFreeSlot(
        f"all {MAX_SLOTS} dev-instance slots are claimed or in use. "
        + "Remove an instance you no longer need from ~/.hephaestus/dev/, "
        + "or stop whatever is holding ports "
        + f"{BACKEND_BASE}-{BACKEND_BASE + MAX_SLOTS - 1}."
    )
