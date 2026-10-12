"""Which zone the operator lives in — asked once, answered from the host.

Odysseus runs on the operator's own machine, so their clock *is* the host's clock. That
sounds like it needs no module, until three callers need the answer in three shapes: the
standing brief wants a name to tell the model, the calendar wants an IANA key it can put
on a row, and neither wants to care that a host can be configured in more than one way.

Three sources, in the order a POSIX host means them:

1. ``TZ`` — an explicit override the operator (or the service manager) set. Where it
   names a zone, it is the answer: everything else on the host is already reading it.
2. ``/etc/localtime`` — on Linux and macOS alike this is a symlink into the tz database,
   and the path tail after ``zoneinfo/`` is the IANA key. This is the common case, and the
   only source that yields a *name* rather than a number.
3. The current UTC offset, as ``UTC±HH:MM``. A last resort: a system with a copied
   rather than linked ``/etc/localtime`` has a real offset and no name to give it, and an
   offset is still worth more to a model reasoning about "tomorrow morning" than nothing.

``TZ`` is a small language rather than a name, and the three spellings POSIX allows are
the reason this module exists at all. A leading colon is permitted and means nothing; an
*empty* value means UTC rather than "unset"; and the value may be a path into the tz
database or a rule string like ``CET-1CEST,M3.5.0`` that names no zone at all. Handing
any of those back verbatim is how the two callers end up describing different clocks —
the brief saying Madrid while the calendar, unable to build a ``ZoneInfo`` from it, files
events in UTC.

**Never raises, and never validates a key it does give back.** A caller that needs one it
can construct a ``ZoneInfo`` from still checks that itself — a zone the tz database
rejects is a different problem from a host that will not say what zone it is in, and the
fallback for one is not the fallback for the other.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path

#: Where a POSIX host records the zone it is set to.
_LOCALTIME = Path("/etc/localtime")

#: The tz database's directory name, wherever the host keeps it — the segment the IANA key
#: follows. Matched from the right, since a key can itself contain the word.
_ZONEINFO = "zoneinfo/"

#: The shape of a tz database key (``UTC``, ``Europe/Madrid``, ``Etc/GMT-14``,
#: ``America/Argentina/Buenos_Aires``). Shape only — the tz database is the authority on
#: whether a well-formed key exists, and this module deliberately does not ask it. What
#: the shape *does* rule out is everything ``TZ`` can hold that is not a name: an absolute
#: path, and a POSIX rule string, both of which carry characters no key does.
_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_+-]*(?:/[A-Za-z0-9_+-]+)*")


def local_zone_key() -> str:
    """The host's timezone, as an IANA key where one can be read and a ``UTC±HH:MM``
    offset where it cannot."""
    declared = os.environ.get("TZ")
    if declared is not None:
        return _zone_from_declaration(declared)

    linked = _zone_from_localtime()
    if linked:
        return linked

    return _offset_label()


def _zone_from_declaration(declared: str) -> str:
    """What ``TZ`` names, reduced to a key or to the offset it puts the host on.

    The fallback here is the offset rather than ``/etc/localtime``, and that is the whole
    point: ``TZ`` is precisely what overrides the link, so a host declaring a rule string
    is on a clock the link does not describe. The offset is read through the same C
    library ``TZ`` configured, so it is the one answer left that still agrees with it.
    """
    value = declared.strip()
    if not value:
        return "UTC"  # POSIX: set-but-empty is UTC, not unset.
    value = value.removeprefix(":")
    if _KEY.fullmatch(value):
        return value
    return _key_after_zoneinfo(value) or _offset_label()


def _zone_from_localtime() -> str | None:
    """The IANA key behind ``/etc/localtime``, or ``None`` when it is not a link into the
    tz database (a copied file, a chroot without one, a host that keeps it elsewhere)."""
    try:
        target = _LOCALTIME.resolve().as_posix()
    except OSError:  # pragma: no cover — resolve() is non-strict; a broken mount could
        return None
    return _key_after_zoneinfo(target)


def _key_after_zoneinfo(path: str) -> str | None:
    """The key a path into the tz database ends with, or ``None`` for any other path."""
    cut = path.rfind(_ZONEINFO)
    if cut < 0:
        return None
    key = path[cut + len(_ZONEINFO) :].strip("/")
    return key or None


def _offset_label() -> str:
    """The host's current offset from UTC, written the way a reader expects to see one."""
    try:
        offset = datetime.now().astimezone().utcoffset()
    except (OSError, ValueError):  # pragma: no cover — a host with no usable clock zone
        offset = None
    if offset is None:
        return "UTC"
    minutes = round(offset.total_seconds() / 60)
    sign = "-" if minutes < 0 else "+"
    hours, remainder = divmod(abs(minutes), 60)
    return f"UTC{sign}{hours:02d}:{remainder:02d}"
