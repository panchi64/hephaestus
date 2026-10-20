"""Conversation-scoped tool auto-approval grants over the ``approval_grants`` table.

When the operator approves a deferred tool call with the "allow for this conversation"
option, a grant is recorded here; while it is active (non-expired) the engine
auto-approves that tool's deferred calls in that conversation instead of re-prompting.
Grants are bounded by a TTL and are visible + revocable. Owner-scoped like every record.

**A grant on a command-running tool names the command.** One yes to ``shell_run_command``
is not a yes to every command a thread will ever want, and read as one it was the widest
hole in the level that gives the operator's approvals for them: a single tick under a test
run switched the per-call decision off for `rm -rf`, `git push` and everything else the
turn went on to try. So a grant on one of :data:`COMMAND_SCOPED_TOOLS` carries the leading
words the command was read as (:func:`services.permissions.command_prefixes`) and covers
only commands whose own leading words are **the same words** — `uv run pytest tests/a.py`
and `uv run pytest -k x` are the act the operator said yes to; `uv run ruff` is not. Every
other tool keeps the whole-tool grant it always had, because there is no narrower thing to
name.

**The words are matched whole, and the act has to be the one that was fenced.** Two
widenings hide inside a scope that reads as narrow, and :func:`covered_by_grant` refuses
both. A scope is only as long as the approved command's own leading words, so *begins
with* would turn one yes to a bare `env` or `uv` into a yes to everything underneath it —
hence equality. And the same leading words run under a different fence are a different
act: a redirect or an environment value pointing outside the worktree, or a wider declared
``reach``, is what decides whether ``tools/shell.py`` confines the process at all, so a
call that widens either is asked about again. Both tests are applied to what is *recorded*
as well as to what is matched, so a scope that could never cover anything is never written
down.

**One whole-tool grant on a command-running tool is still written, deliberately.** A
scheduled task's ``pre_authorized`` list (``harness/manifests/tasks.py``) seeds a grant per
tool name into the fresh conversation each unattended execution opens. There is no command
there to name — the operator wrote the tool name on the task itself, ahead of any turn —
so the empty scope is the honest record of what they authorized, and narrowing it would
leave an unattended task parked forever on the tool it was configured to use.

**And the operator can ask for that wider thing themselves, which is what ``decisive``
records.** Scoping a grant to the command is the right default and the wrong *only* option:
a thread doing a morning's work in a shell hits a fresh prompt per command, and the operator
who has decided to stop reading them has no way to say so. So the approval card offers a
second, separately-worded width — this tool, any command — and a grant recorded from it is
marked decisive. The two are different objects to :func:`grant_width`, and the difference
matters at exactly one level: the one that *reviews* rather than asking outright. There a
command-scoped grant is an input to the review and a decisive one is the operator's
authorization, which is the whole of what they were offered. Neither buys an act nobody can
undo, and neither stands in for a review that could not run.

A grant is operator policy, not secret content, so it is stored in the clear (no vault
sealing). Expiry is enforced in Python after normalizing to UTC, so the round-trip
through SQLite (which may drop tzinfo) can't make a naive/aware comparison raise.
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
    declared_reach,
)

#: The tools whose grant is scoped to a command rather than to the whole tool — the ones
#: whose call carries the command line the operator is really answering. Named as literals
#: because ``services/`` sits below ``tools/``; ``tests/test_approval_grants.py`` pins the
#: set against the catalog's own so a new executing tool cannot quietly inherit a
#: whole-tool grant.
COMMAND_SCOPED_TOOLS = frozenset(
    {"shell_run_command", "shell_start_command", "code_run_host_command"}
)

#: The argument every one of those tools carries the command line in.
_COMMAND_ARGUMENT = "command"

_WORDS = TypeAdapter(list[str])

#: Tools no grant may ever cover, whatever scope was asked for. A grant auto-approves a
#: *tool name*, and nothing narrower — so a standing one on the egress request would mean
#: every domain the agent goes on to name (the approved domain is remembered by the egress
#: policy instead), and one on a plan approval would let the agent raise its own level.
#: Both writers of a grant consult it, and :func:`grant_width` ignores any row on one.
ONCE_ONLY_TOOLS = frozenset({"code_request_egress"}) | OPERATOR_ONLY_TOOLS


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
    """A command scope as the column stores it — a JSON list, or ``""`` for the whole tool.

    JSON rather than the words joined by spaces, even though the walk can never hand back
    a word carrying whitespace (one that does is unbounded and yields no prefix at all):
    the column is what a UNIQUE index and an upsert's ``ON CONFLICT`` compare, and an
    encoding that is only unambiguous because of an invariant two files away is one that
    stops being unambiguous the day that invariant moves.
    """
    return json.dumps(list(words)) if words else ""


def decode_prefix(stored: str) -> tuple[str, ...]:
    """The words a stored scope names — the inverse of :func:`encode_prefix`.

    A value this module did not write (a hand-edited row, a restored backup from a
    different shape) reads back as a single opaque word, which matches no command rather
    than as the *empty* scope, which would cover the whole tool. The failure direction
    matters more than the case is likely.
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

    ``None`` when the call runs a command no scope could stand for: one the grammar could
    not read, or one naming a path outside the worktree. There is nothing to name in the
    first and nothing a *later* call could safely be measured against in the second — and
    the honest answer to both is to record no grant at all. Falling back to a whole-tool
    grant would hand exactly those commands the standing yes this scoping exists to
    withhold.

    A reach wider than the tool's own is *part of the scope* rather than a reason to record
    nothing (:func:`_reach_marker`). At Auto the only shell calls that ever park are the
    ones declaring ``host`` or ``network`` — so a scoping that refused them would leave the
    operator's "allow for this conversation" with nothing it could ever apply to.
    """
    if tool_name not in COMMAND_SCOPED_TOOLS:
        return [()]
    scopes = _scopes_of(tool_name, args)
    if scopes is None:
        return None
    # Deduplicated, order preserved: `git add . && git commit` is one act named twice.
    return list(dict.fromkeys(scopes))


def _scopes_of(tool_name: str, args: Mapping[str, object]) -> list[tuple[str, ...]] | None:
    """Every scope this call's command line names, or None when it names none.

    One per stage of the pipeline, each led by the reach marker where the call declared a
    reach wider than its tool's own. Used on both sides — the scope a grant *records* and
    the scopes a later call is *matched* by — so the two are the same reading by
    construction.
    """
    command = args.get(_COMMAND_ARGUMENT)
    if not isinstance(command, str):
        return None
    prefixes = command_prefixes(command)
    if not prefixes:
        return None
    marker = _reach_marker(tool_name, args)
    return [(*marker, *prefix) for prefix in prefixes]


def grant_width(
    tool_name: str | None, args: Mapping[str, object], grants: Sequence[GrantInfo]
) -> GrantWidth:
    """How far this conversation's active grants reach over one deferred call.

    The single rule consulted by the engine's park-time split, the review it hands a call
    to, and the approve route's resume-time re-validation — so no two of them can diverge
    about what a grant reaches.

    ``"tool"`` where a grant the operator marked as their answer for the whole tool covers
    the call: a decisive whole-tool grant, and only that. ``"command"`` where the call is
    covered by a grant that names the act instead — one recorded from an approval, or an
    undecisive whole-tool row (a scheduled task's seed, or a thread granted before the
    wider option existed). ``"none"`` where nothing covers it.

    **The decisive reading is never inferred from an empty scope alone.** Emptiness is a
    shape several writers produce; being answered for is a thing only the operator says.

    A whole-tool grant covers any call to that tool. A command-scoped one covers a command
    **every** stage of which leads with exactly some granted scope's words — every stage,
    because a pipeline runs all of them and a grant on its head is not consent to its tail;
    and *exactly*, because a scope is only as long as the approved command's own leading
    words, so a scope of `("env",)` matched as a prefix would cover `env rm -rf /tmp/x`
    and `env FOO=1 curl …` alike. Equality loses nothing the scoping was for: both sides
    are read by the same walk, so `("uv", "run", "pytest")` still equals what `uv run
    pytest -k x` leads with. The words include the command's **flags**
    (:func:`services.permissions.command_prefixes`), which is what stops one yes to `curl
    -sS <url>` from standing for `curl -d @.env <elsewhere>`: an option is part of the act,
    so an invocation flagged differently is a different scope and is asked about again.

    Nothing is covered where the act itself has moved. A command the grammar could not read
    is covered by nothing (the word that decides the act may be the one that could not be
    read), nor is one naming a path outside the worktree, nor one declaring a reach wider
    than its tool's own — the last two are how the same leading words stop running inside
    the fence, which is most of what the operator was agreeing to.
    """
    if tool_name is None or tool_name in ONCE_ONLY_TOOLS:
        return "none"
    on_tool = [grant for grant in grants if grant.tool_name == tool_name]
    if not on_tool:
        return "none"
    whole_tool = [grant for grant in on_tool if not grant.command_prefix]
    if whole_tool:
        return "tool" if any(grant.decisive for grant in whole_tool) else "command"
    called = _scopes_of(tool_name, args)
    if not called:
        return "none"
    scopes = [grant.command_prefix for grant in on_tool]
    if not all(scope in scopes for scope in called):
        return "none"
    # A command-scoped grant names one act, so it is never the answer to every call a tool
    # could make — whatever width it was recorded at, it informs rather than settles.
    return "command"


def covered_by_grant(
    tool_name: str | None, args: Mapping[str, object], grants: Sequence[GrantInfo]
) -> bool:
    """Whether a deferred call is covered by a conversation's active grants at all.

    What the levels that ask outright want to know — there a grant of either width settles
    the call, because the question those levels are asking is the one the operator already
    answered. The width only separates them where a *review* is doing the deciding
    (:func:`grant_width`), and this is deliberately a reading of that same answer rather
    than a second walk over the grants.
    """
    return grant_width(tool_name, args, grants) != "none"


def _reach_marker(tool_name: str, args: Mapping[str, object]) -> tuple[str, ...]:
    """The word a scope leads with when the call declared a reach wider than its tool's own.

    ``reach`` is the model's own argument on the executing shell tools, and it is what
    decides the fence the command runs under — ``host`` runs unwrapped, ``network`` opens
    egress. So `uv run pytest` declared ``host`` is not the act the operator ticked a box
    under a fenced run of; it is that command with the fence taken off, which is the part
    of the yes that was doing the work. The marker makes the two different scopes: a grant
    recorded under a ``host`` run covers ``host`` runs of the same words and nothing
    narrower or wider, and a grant recorded under the fence never covers a run without it.

    Spelled ``@host`` rather than as a bare word so it cannot collide with a program name,
    and only present where the reach *is* wider — compared against the tool's own answer
    with no arguments rather than against the literal ``"offline"``, because a tool with
    no such argument declares the same thing on every call and has nothing to widen:
    `code_run_host_command` reaches the host by construction, and marking every one of its
    scopes would say nothing a reader did not already know.
    """
    reach = declared_reach(tool_name, dict(args))
    if reach is None or reach == declared_reach(tool_name, {}):
        return ()
    return (f"@{reach}",)


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

        ``decisive`` is the operator's wider pick — their answer for the whole tool, taken
        as the authorization by the level that reviews. It defaults off, so the two writers
        that never carry it (an approval's command-scoped yes, a scheduled task's seed) go
        on recording exactly what they always did.
        """
        expires_at = utcnow() + self._ttl
        scope = encode_prefix(command_prefix)

        def work(session: Session) -> datetime:
            # Atomic get-or-create over uq_approval_grant_scope: a plain select-then-insert
            # races two concurrent approvals of the same tool into a duplicate-insert
            # IntegrityError, so push the conflict resolution into the DB — insert, or
            # refresh the existing row's expiry on conflict.
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
                    # The width moves with the expiry, so ticking the wider option on a
                    # tool already granted promotes the row the operator can see rather
                    # than writing a second one beside it. It is outside the unique index
                    # for exactly that reason: two grants over the same scope disagreeing
                    # about how far it reaches is not a state worth being able to hold.
                    #
                    # **It promotes and never narrows.** A conflicting write that does not
                    # ask for the wider width is not the operator withdrawing it — it is a
                    # refresh, or the other half of a batch that settled two calls to one
                    # tool, and there the row written last would otherwise decide how far
                    # the grant reaches. Narrowing is something they do on purpose, by
                    # revoking the chip, which drops the row outright.
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
            # Expiry is compared in Python (tz-normalized) so a SQLite-naive round-trip
            # can't break the comparison; a SQL `WHERE expires_at > now` would. Lapsed
            # rows are pruned opportunistically on read so the table can't grow without
            # bound — a bulk delete by id, idempotent under a concurrent prune. A row on a
            # once-only tool covers nothing, so it is pruned too rather than listed.
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
