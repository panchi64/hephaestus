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
task's ``pre_authorized`` list (``harness/manifests/tasks.py``) seeds one per tool name;
there is no command to name ahead of any turn, and a narrower grant would park the task
forever.

**``decisive`` records the operator's wider pick.** The approval card offers a second
width, this tool with any command. The two differ to :func:`grant_width` only at the level
that *reviews*: a command-scoped grant is an input to the review, a decisive one is the
operator's authorization. Neither covers an irreversible act or replaces a review that
could not run.

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
    OPERATOR_ONLY_TOOLS,
    GrantWidth,
    command_prefixes,
)
from services.permissions.guards import is_guarded

#: Literals because ``services/`` sits below ``tools/``; ``tests/test_approval_grants.py``
#: pins the set against the catalog's so a new executing tool cannot inherit a whole-tool
#: grant.
COMMAND_SCOPED_TOOLS = frozenset({"shell_run_command", "shell_start_command", "shell_run_on_host"})

_COMMAND_ARGUMENT = "command"

_WORDS = TypeAdapter(list[str])

#: Tools no grant may cover. A grant approves a tool name only, so one on the egress request
#: would cover every domain (the egress policy remembers approved domains instead) and one
#: on a plan approval would let the agent raise its own level. Both grant writers consult
#: it, and :func:`grant_width` ignores any row on one.
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
    #: Whether this grant answers for the operator at the level that reviews, or only
    #: informs it. See :func:`grant_width`.
    decisive: bool = False


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


def grant_width(
    tool_name: str | None, args: Mapping[str, object], grants: Sequence[GrantInfo]
) -> GrantWidth:
    """How far this conversation's active grants reach over one deferred call.

    The single rule shared by the engine's park-time split, the review a call is handed to,
    and the approve route's resume-time re-validation.

    ``"tool"``: a decisive whole-tool grant covers the call, and only that. ``"command"``:
    a grant naming the act covers it, or an undecisive whole-tool row (a scheduled task's
    seed, or a thread granted before the wider option existed). ``"none"``: nothing does.
    Decisiveness is never inferred from an empty scope; only the operator says it.

    A whole-tool grant covers any call to its tool. A command-scoped one covers a command
    **every** stage of which leads with **exactly** some granted scope's words: every
    stage, since a grant on a pipeline's head is not consent to its tail; exactly, since a
    scope of `("env",)` matched as a prefix would cover `env rm -rf /tmp/x`. The words
    include flags (:func:`services.permissions.command_prefixes`), so a yes to `curl -sS
    <url>` does not stand for `curl -d @.env <elsewhere>`.

    Nothing covers a command the grammar could not read or one naming a path outside the
    worktree.
    """
    if tool_name is None or tool_name in ONCE_ONLY_TOOLS:
        return "none"
    on_tool = [grant for grant in grants if grant.tool_name == tool_name]
    if not on_tool:
        return "none"
    whole_tool = [grant for grant in on_tool if not grant.command_prefix]
    if whole_tool:
        return "tool" if any(grant.decisive for grant in whole_tool) else "command"
    called = _scopes_of(args)
    if not called:
        return "none"
    scopes = [grant.command_prefix for grant in on_tool]
    if not all(scope in scopes for scope in called):
        return "none"
    # A command-scoped grant names one act, so it informs a review whatever width it has.
    return "command"


def covered_by_grant(
    tool_name: str | None, args: Mapping[str, object], grants: Sequence[GrantInfo]
) -> bool:
    """Whether a deferred call is covered by a conversation's active grants at all.

    For the levels that ask outright, a grant of either width settles the call; width only
    matters where a *review* decides (:func:`grant_width`, which this reads).
    """
    return grant_width(tool_name, args, grants) != "none"


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
        decisive: bool = False,
    ) -> datetime:
        """Record (or refresh) a conversation-scoped auto-approval for ``tool_name``,
        scoped to ``command_prefix`` where the tool runs a command. Returns the new
        expiry.

        ``decisive`` is the operator's wider pick: their answer for the whole tool, taken as
        the authorization by the level that reviews.
        """
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
                    decisive=decisive,
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
                    # ``decisive`` is outside the unique index so the wider option promotes
                    # the existing row instead of adding a second. It promotes and never
                    # narrows: a conflicting write without it is a refresh or half a batch,
                    # not a withdrawal; revoking the chip is how the operator narrows.
                    set_=(
                        {"expires_at": expires_at, "decisive": True}
                        if decisive
                        else {"expires_at": expires_at}
                    ),
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
                            decisive=r.decisive,
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
