"""Conversation-scoped tool auto-approval grants over the ``approval_grants`` table.

An "allow for this conversation" approval records a grant; while it is unexpired the
engine auto-approves that tool's deferred calls in the conversation. Grants carry a TTL and
are visible and revocable. Stored in the clear: a grant is policy, not secret content.

**A grant on a command-running tool names the command.** A grant on one of
:data:`COMMAND_SCOPED_TOOLS` carries the leading words of the approved command
(:func:`services.permissions.command_prefixes`) and covers only commands leading with
**the same words**, so a yes to `uv run pytest` is not a yes to `rm -rf` or `uv run ruff`.
Other tools take a whole-tool grant.

**Words are matched whole, under the same fence.** :func:`covered_by_grant` refuses two
widenings: prefix matching (a yes to a bare `env` would cover everything beneath it), and
the same words naming a path outside the worktree. Both tests apply when a scope is
recorded too, so a scope that could never match is never written.

**One whole-tool grant on a command-running tool is written deliberately.** A scheduled
task's ``pre_authorized`` list (``plugins/routines/executor.py``) seeds one per tool name;
there is no command to name ahead of any turn, and a narrower grant would park the task
forever.

A grant settles the call it covers at every level that asks, Auto included, where it
clears what the VM does not bound (``services/permissions/decide.py``). A level's
refusal it never overturns.

Expiry is compared in Python after normalizing to UTC, because SQLite may drop tzinfo.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from pydantic import TypeAdapter
from sqlalchemy import Engine, delete
from sqlmodel import Session, col, select

from core.db import in_session, upsert
from core.serde import as_utc
from models._fields import new_id, utcnow
from models.approval_grant import ApprovalGrant
from services.permissions import (
    HOST_TOOLS,
    OPERATOR_ONLY_TOOLS,
    VM_SHELL_TOOLS,
    command_prefixes,
)
from services.permissions.guards import is_guarded

#: ``tests/test_approval_grants.py`` pins the set against the catalog's so a new executing
#: tool cannot inherit a whole-tool grant.
COMMAND_SCOPED_TOOLS = VM_SHELL_TOOLS | HOST_TOOLS

_COMMAND_ARGUMENT = "command"

_WORDS = TypeAdapter(list[str])

#: Tools no grant may cover. A grant approves a tool name only, so one on the egress request
#: would cover every domain (the egress policy remembers approved domains instead) and one
#: on a plan approval would let the agent raise its own level. Both grant writers consult
#: it, and :func:`covered_by_grant` ignores any row on one.
ONCE_ONLY_TOOLS = frozenset({"code_request_egress"}) | OPERATOR_ONLY_TOOLS


def grantable(tool_name: str, metadata: Mapping[str, object] | None = None) -> bool:
    """Whether an approval of this call may leave a standing grant behind. A guarded call
    (``metadata`` carries its guard) is once-only whatever its tool."""
    return tool_name not in ONCE_ONLY_TOOLS and not is_guarded(metadata)


@dataclass(frozen=True)
class GrantInfo:
    """A live grant, for the operator's visible/revocable list."""

    tool_name: str
    expires_at: datetime
    #: The leading words this grant is scoped to (`("uv", "run", "pytest")`), empty for a
    #: grant over the whole tool.
    command_prefix: tuple[str, ...] = ()


def encode_prefix(words: Sequence[str]) -> str:
    """A command scope as the column stores it: a JSON list, or ``""`` for the whole tool.

    JSON rather than space-joined, so the UNIQUE index and the upsert's ``ON CONFLICT``
    stay unambiguous without relying on words never containing whitespace.
    """
    return json.dumps(list(words)) if words else ""


def decode_prefix(stored: str) -> tuple[str, ...]:
    """The words a stored scope names — the inverse of :func:`encode_prefix`.

    A value this module did not write reads back as one opaque word that matches no
    command, never as the empty scope, which would cover the whole tool.
    """
    if not stored:
        return ()
    try:
        return tuple(_WORDS.validate_json(stored))
    except ValueError:
        return (stored,)


def grant_scopes(tool_name: str, args: Mapping[str, object]) -> list[tuple[str, ...]] | None:
    """The scopes a "for this conversation" approval of this call should record.

    One empty scope — the whole tool — for everything but a command-running tool. For one
    of those, one scope per stage of the command line: `git diff | grep x` is two acts, and
    a grant naming only the first would never cover the call it was recorded from.

    ``None`` when the command is one the grammar could not read or names a path outside the
    worktree: no grant is recorded, since a whole-tool fallback would give those commands
    exactly the standing yes this scoping withholds.

    """
    if tool_name not in COMMAND_SCOPED_TOOLS:
        return [()]
    scopes = _scopes_of(args)
    if scopes is None:
        return None
    # Deduplicated, order preserved: `git add . && git commit` is one act named twice.
    return list(dict.fromkeys(scopes))


def _scopes_of(args: Mapping[str, object]) -> list[tuple[str, ...]] | None:
    """Every scope this call's command line names (one per pipeline stage), or None.

    Shared by recording and matching so the two read a command identically.
    """
    command = args.get(_COMMAND_ARGUMENT)
    if not isinstance(command, str):
        return None
    prefixes = command_prefixes(command)
    return list(prefixes) if prefixes else None


def covered_by_grant(
    tool_name: str | None, args: Mapping[str, object], grants: Sequence[GrantInfo]
) -> bool:
    """Whether this conversation's active grants cover one deferred call: the rule shared
    by the engine's park-time split and the approve route's resume-time re-check.

    A whole-tool grant covers any call to its tool. A command-scoped one covers a command
    **every** stage of which leads with **exactly** some granted scope's words: every stage,
    since a grant on a pipeline's head is not consent to its tail; exactly, since a scope of
    `("env",)` matched as a prefix would cover `env rm -rf /tmp/x`. The words include flags
    (:func:`services.permissions.command_prefixes`), so a yes to `curl -sS <url>` does not
    stand for `curl -d @secrets <elsewhere>`. Nothing covers a command the grammar could not
    read or one naming a path outside the working directory.
    """
    if tool_name is None or tool_name in ONCE_ONLY_TOOLS:
        return False
    on_tool = [grant for grant in grants if grant.tool_name == tool_name]
    if any(not grant.command_prefix for grant in on_tool):
        return True
    called = _scopes_of(args)
    if not on_tool or not called:
        return False
    scopes = [grant.command_prefix for grant in on_tool]
    return all(scope in scopes for scope in called)


class ApprovalGrantStore:
    def __init__(self, db_engine: Engine, ttl_s: float) -> None:
        self._db: Engine = db_engine
        self._ttl: timedelta = timedelta(seconds=ttl_s)

    async def grant(
        self,
        owner_id: str,
        conversation_id: str,
        tool_name: str,
        command_prefix: Sequence[str] = (),
    ) -> datetime:
        """Record (or refresh) a conversation-scoped auto-approval for ``tool_name``,
        scoped to ``command_prefix`` where the tool runs a command. Returns the new
        expiry."""
        expires_at = utcnow() + self._ttl
        scope = encode_prefix(command_prefix)

        def work(session: Session) -> datetime:
            # Upsert over uq_approval_grant_scope: select-then-insert would race two
            # concurrent approvals of one tool into an IntegrityError.
            stmt = (
                upsert(self._db, ApprovalGrant)
                .values(
                    id=new_id(),
                    owner_id=owner_id,
                    conversation_id=conversation_id,
                    tool_name=tool_name,
                    command_prefix=scope,
                    created_at=utcnow(),
                    expires_at=expires_at,
                )
                .on_conflict_do_update(
                    index_elements=[
                        "owner_id",
                        "conversation_id",
                        "tool_name",
                        "command_prefix",
                    ],
                    set_={"expires_at": expires_at},
                )
            )
            _ = session.exec(stmt)
            return expires_at

        return await in_session(self._db, work)

    async def list(self, owner_id: str, conversation_id: str) -> list[GrantInfo]:
        """Live (non-expired) grants in this conversation — the operator's revocable view,
        and what both grant-coverage checks are answered against."""
        if not conversation_id:
            return []
        now = utcnow()

        def work(session: Session) -> list[GrantInfo]:
            rows = session.exec(
                select(ApprovalGrant)
                .where(ApprovalGrant.owner_id == owner_id)
                .where(ApprovalGrant.conversation_id == conversation_id)
            ).all()
            live: list[GrantInfo] = []
            expired_ids: list[str] = []
            # Expiry is compared in Python because SQLite returns naive datetimes. Lapsed
            # rows, and rows on once-only tools, are pruned on read by an idempotent bulk
            # delete.
            for r in rows:
                if as_utc(r.expires_at) > now and r.tool_name not in ONCE_ONLY_TOOLS:
                    live.append(
                        GrantInfo(
                            tool_name=r.tool_name,
                            expires_at=as_utc(r.expires_at),
                            command_prefix=decode_prefix(r.command_prefix),
                        )
                    )
                else:
                    expired_ids.append(r.id)
            if expired_ids:
                _ = session.exec(
                    delete(ApprovalGrant).where(col(ApprovalGrant.id).in_(expired_ids))
                )
            return live

        return await in_session(self._db, work)

    async def revoke(
        self,
        owner_id: str,
        conversation_id: str,
        tool_name: str,
        command_prefix: Sequence[str] = (),
    ) -> None:
        """Drop one grant — the next call it covered asks again."""
        scope = encode_prefix(command_prefix)

        def work(session: Session) -> None:
            row = session.exec(
                select(ApprovalGrant)
                .where(ApprovalGrant.owner_id == owner_id)
                .where(ApprovalGrant.conversation_id == conversation_id)
                .where(ApprovalGrant.tool_name == tool_name)
                .where(ApprovalGrant.command_prefix == scope)
            ).first()
            if row is not None:
                session.delete(row)

        await in_session(self._db, work)
