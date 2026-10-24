"""The network allowlist a conversation's machine is booted with.

A workspace's allowlist is the installation-wide set (``egress_allowed_domains``) plus
the domains the operator approved for that conversation, stored in the database under the
workspace's scratch key. A stateless run writes rows too, since this module cannot tell a
run id from a conversation id. A grant restarts the conversation's machine on its next
command (``services/vm/machines.py``).
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable

from sqlalchemy import Engine, delete
from sqlmodel import Session, col, select

from core.db import in_session, upsert
from core.exceptions import InvalidInputError
from models._fields import new_id, utcnow
from models.egress_grant import EgressGrant

# Single-operator ownership seam; the column exists so a second owner is a code change,
# not a migration.
_OWNER_ID = "operator"

# One DNS label. Matched with `fullmatch` because `$` also matches before a trailing
# newline, which would become two lines in the file the fences read.
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


def normalise_domain(raw: str) -> str:
    """One supplied domain reduced to the token the allowlist stores.

    Everything that is not the host is dropped rather than refused, since a model asked for
    a domain often hands over a URL.

    Refused is anything whose allowlist meaning is wider than it reads: an empty string;
    an address in any spelling a resolver accepts (``1.2.3.4``, ``127.1``, ``2130706433``),
    since the fences match names and an address walks around a name check; a bare top
    level (``com``, ``*.com``); any wildcard but a leading ``*.`` (``*``, ``a.*.com``); and
    a ``@``, because the host of ``pypi.org@evil.example.com`` is the half nobody reads.
    """
    text = raw.strip().lower()
    if not text:
        raise InvalidInputError("an egress domain cannot be empty")
    scheme, marker, rest = text.partition("://")
    # The first `://`, and only with no path before it: a URL embedding another in its
    # query would otherwise yield the embedded host, not the one the operator approved.
    if marker and not any(c in scheme for c in "/?#"):
        text = rest
    for separator in ("/", "?", "#"):  # path, query, fragment
        text = text.split(separator, 1)[0]
    if "@" in text:
        # Userinfo puts the real host after the `@`, so the approval card, which echoes
        # the model's text, would read as a different host from the one granted.
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
    # Two labels minimum under a letter-initial top level: `com` is a whole top level, and
    # `127.1` or `2130706433` is an address a resolver expands. The host confinement also
    # refuses `*.com` outright.
    if len(labels) < 2 or labels[-1][0].isdigit():
        message = (
            f"{raw!r} does not name a host: an allowlist entry is a domain under a "
            "top-level domain, like `example.com`"
        )
        raise InvalidInputError(message)
    return f"*.{name}" if wildcard else name


class EgressPolicy:
    """What each workspace may reach."""

    def __init__(self, db_engine: Engine, global_domains: tuple[str, ...]) -> None:
        self._db: Engine = db_engine
        # Same normalisation as approved domains, so the union holds one spelling per host.
        # A malformed configured domain raises at boot rather than being silently dropped.
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

        Every domain is normalised first, so a batch with one unusable entry is refused
        whole; the operator approved a set, not a part of it.

        The fences' file is rewritten here because the grant unblocks a request about to be
        retried, and a stale file would refuse it.
        """
        if not key:
            raise InvalidInputError("egress is granted to a workspace; none was named")
        wanted = sorted({normalise_domain(d) for d in domains})
        if not wanted:
            return await self.allowed_for(key)

        def work(session: Session) -> None:
            # Conflict handled in the database: select-then-insert would race two approvals
            # of one domain into a duplicate-key error.
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
        return await self.allowed_for(key)

    async def forget(self, conversation_id: str) -> None:
        """Drop a conversation's grants and its materialised allowlist, on deletion."""
        if not conversation_id:
            return

        def work(session: Session) -> None:
            _ = session.exec(
                delete(EgressGrant)
                .where(col(EgressGrant.owner_id) == _OWNER_ID)
                .where(col(EgressGrant.conversation_id) == conversation_id)
            )

        await in_session(self._db, work)
