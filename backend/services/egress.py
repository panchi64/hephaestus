"""The one network allowlist every agent workspace is fenced by.

Compute is not the thing worth restricting — installing a package, cloning a repository
and burning CPU are what the workspace is *for*, and a fence that makes them feel
dangerous only teaches the operator to turn it off. Exfiltration is. So there is exactly
one policy here, expressed as domains, and both fences read it: the VM's egress
gateway and the OS-level confinement the code-mode shell and the host hatch run under.
Two lists would mean a hole in whichever one nobody remembered to widen.

A workspace's allowlist is the installation-wide set (``egress_allowed_domains``) plus
whatever the operator approved for that particular conversation. The per-conversation
half is a durable record — the operator read a domain and agreed to it — so it lives in
the database, keyed by the same workspace key the session manager uses. A stateless run
writes rows there too: telling a run id from a conversation id is not something this
module can do, and the alternative (a second in-memory path) would be a second set of
semantics for the sake of a handful of rows that no longer name anything once the run
ends.

The fences do not read the database. They read a file — ``allow.txt``, one domain per
line — inside a **directory** this module materialises per workspace. The gateway re-reads
the file whenever its mtime moves, so a grant takes effect on the next connection.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import secrets
import shutil
from collections.abc import Iterable
from pathlib import Path

from sqlalchemy import Engine, delete
from sqlmodel import Session, col, select

from core.db import in_session, upsert
from core.exceptions import InvalidInputError
from models._fields import new_id, utcnow
from models.egress_grant import EgressGrant
from services.sandbox.base import safe_key
from services.vm.egress import ALLOW_FILE

# The ownership seam, single-operator like every other record. Not a parameter because
# nothing calling this has a second owner to hand it; the column exists so that the day a
# second human does, this is a code change and not a migration.
_OWNER_ID = "operator"

# One DNS label: letters/digits/hyphen, never leading or trailing a hyphen. Matched with
# `fullmatch`, because `$` would also match before a trailing newline — and a domain
# smuggling one past validation becomes two lines in the file the fences read.
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


def normalise_domain(raw: str) -> str:
    """One supplied domain reduced to the token the allowlist stores.

    Everything that is not the host is *dropped* rather than refused. A model asked to
    name a domain hands over a URL as often as not, and spending an approval round-trip
    on ``https://pypi.org/simple`` teaches nothing to anybody.

    What is refused is anything whose allowlist meaning would be wider than it reads: an
    empty string; an address rather than a name, in any of the spellings a resolver
    accepts (``1.2.3.4``, ``127.1``, ``2130706433``) — the fences match names, and a bare
    address is precisely how a name check gets walked around; a bare top level (``com``,
    ``*.com``), which is most of the web behind a card that reads like one host; any
    wildcard but a leading ``*.`` — ``*`` and ``a.*.com`` look like one host and match a
    great many; and a ``@``, because the host of ``pypi.org@evil.example.com`` is the half
    nobody reads, and the whole content of this approval is *which host*.
    """
    text = raw.strip().lower()
    if not text:
        raise InvalidInputError("an egress domain cannot be empty")
    scheme, marker, rest = text.partition("://")
    # The *first* `://`, and only when nothing of a path precedes it. A URL that carries
    # another URL in its query or fragment has two, and taking the later one would read
    # the host out of the embedded link — the operator approves `pypi.org` and the
    # allowlist quietly gains whatever was hidden after the `?`.
    if marker and not any(c in scheme for c in "/?#"):
        text = rest
    for separator in ("/", "?", "#"):  # path, query, fragment
        text = text.split(separator, 1)[0]
    if "@" in text:
        # Not dropped the way a path is. Userinfo puts the real host *after* the `@`, so
        # `pypi.org@evil.example.com` grants one host while the approval card — which
        # echoes what the model asked for — reads as another.
        raise InvalidInputError(
            f"{raw!r} carries a `@`: name the host on its own, without credentials"
        )
    if text.startswith("["):  # a bracketed IPv6 literal, refused below
        host = text[1:].partition("]")[0]
    else:
        host = text.split(":", 1)[0]  # port
    host = host.rstrip(".")  # the DNS root's trailing dot
    wildcard = host.startswith("*.")
    name = host.removeprefix("*.") if wildcard else host
    if not name or "*" in name:
        raise InvalidInputError(
            f"{raw!r} is not a domain: name one host, or one `*.example.com` wildcard"
        )
    try:
        _ = ipaddress.ip_address(name)
    except ValueError:
        pass
    else:
        raise InvalidInputError(f"{raw!r} is an IP address; the allowlist matches domain names")
    labels = name.split(".")
    if not all(_LABEL.fullmatch(label) for label in labels):
        raise InvalidInputError(f"{raw!r} is not a valid domain name")
    # Two labels at minimum, under a top level that starts with a letter. Both rules
    # refuse a name that matches far wider than it reads: `com` (or `*.com`) is a whole
    # top level from one approval card, and `127.1` or `2130706433` is an address that a
    # resolver expands but a reader does not see as one. `*.com` is also a pattern the
    # host confinement refuses outright, and that fence has to come up.
    if len(labels) < 2 or labels[-1][0].isdigit():
        message = (
            f"{raw!r} does not name a host: an allowlist entry is a domain under a "
            "top-level domain, like `example.com`"
        )
        raise InvalidInputError(message)
    return f"*.{name}" if wildcard else name


class EgressPolicy:
    """What each workspace may reach, and the file the fences read it from."""

    def __init__(self, db_engine: Engine, data_dir: Path, global_domains: tuple[str, ...]) -> None:
        self._db: Engine = db_engine
        self._root: Path = Path(data_dir) / "sandbox" / "egress"
        # Normalised through the same funnel as an approved domain, so the union below
        # can't hold two spellings of one host. A malformed configured domain raises here,
        # at boot: an operator who wrote one believes it is allowed, and silently dropping
        # it would leave them debugging the fence instead of their typo.
        self._global: frozenset[str] = frozenset(normalise_domain(d) for d in global_domains)

    async def allowed_for(self, key: str) -> frozenset[str]:
        """Everything the workspace behind ``key`` may reach."""
        if not key:
            return self._global

        def work(session: Session) -> set[str]:
            rows = session.exec(
                select(EgressGrant)
                .where(EgressGrant.owner_id == _OWNER_ID)
                .where(EgressGrant.conversation_id == key)
            ).all()
            return {row.domain for row in rows}

        return self._global | frozenset(await in_session(self._db, work))

    async def allow(self, key: str, domains: Iterable[str]) -> frozenset[str]:
        """Grant ``domains`` to this workspace and return its whole allowlist.

        Every domain is normalised before anything is written, so a batch carrying one
        unusable entry is refused whole rather than half-applied — the operator approved
        a set, and a partial grant is not the set they read.

        The file the fences read is rewritten here, not left to the next thing that
        happens to call :meth:`materialise`. A grant exists to unblock a request that is
        about to be retried, and a fence still reading the allowlist as it stood before
        the approval refuses that retry — which reads, to whoever is watching, as an
        approval that did nothing.
        """
        if not key:
            raise InvalidInputError("egress is granted to a workspace; none was named")
        wanted = sorted({normalise_domain(d) for d in domains})
        if not wanted:
            return await self.allowed_for(key)

        def work(session: Session) -> None:
            # Conflict resolution in the database rather than a select-then-insert: two
            # approvals naming the same domain race into a duplicate-key error otherwise,
            # and there is nothing to update — the row already says what it should.
            for domain in wanted:
                _ = session.exec(
                    upsert(self._db, EgressGrant)
                    .values(
                        id=new_id(),
                        owner_id=_OWNER_ID,
                        conversation_id=key,
                        domain=domain,
                        created_at=utcnow(),
                    )
                    .on_conflict_do_nothing(
                        index_elements=["owner_id", "conversation_id", "domain"]
                    )
                )

        await in_session(self._db, work)
        _ = await self.materialise(key)
        return await self.allowed_for(key)

    async def forget(self, conversation_id: str) -> None:
        """Drop a conversation's grants and its materialised allowlist.

        Called when the conversation is deleted: the domains restate what the operator
        was asked about in a thread that no longer exists, and the directory on disk is
        derived from them.
        """
        if not conversation_id:
            return

        def work(session: Session) -> None:
            _ = session.exec(
                delete(EgressGrant)
                .where(col(EgressGrant.owner_id) == _OWNER_ID)
                .where(col(EgressGrant.conversation_id) == conversation_id)
            )

        await in_session(self._db, work)
        directory = self.allow_dir(conversation_id)
        await asyncio.to_thread(lambda: shutil.rmtree(directory, ignore_errors=True))

    def allow_dir(self, key: str) -> Path:
        """The directory holding this workspace's allowlist file. Derived from the same
        ``safe_key`` the session manager names workspaces with, so a
        workspace and its allowlist can never end up under two different tokens."""
        return self._root / safe_key(key)

    async def materialise(self, key: str) -> Path:
        """Write this workspace's allowlist and return the directory holding it."""
        domains = sorted(await self.allowed_for(key))
        directory = self.allow_dir(key)
        await asyncio.to_thread(_write_allow_file, directory, domains)
        return directory


def _write_allow_file(directory: Path, domains: list[str]) -> None:
    """``allow.txt``, replaced whole. A reader (the proxy re-reads it on each request)
    either sees the previous list or the new one, never a half-written file — so the
    write goes to a temp file in the same directory, which puts it on the same
    filesystem, and lands with a single ``os.replace``."""
    directory.mkdir(parents=True, exist_ok=True)
    tmp = directory / f".{ALLOW_FILE}.{os.getpid()}.{secrets.token_hex(4)}"
    _ = tmp.write_text("".join(f"{domain}\n" for domain in domains), encoding="utf-8")
    os.replace(tmp, directory / ALLOW_FILE)
