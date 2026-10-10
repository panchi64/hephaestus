"""Turning the retired research rows into the threads they should always have been.

Research was an entity: a question, a frozen plan and a finished report on a row of its
own, read back through its own REST surface. It is a conversation in research mode now,
and the operator's existing rows are not scaffolding to be discarded — a report is often
hours of reading they asked for and paid for.

The migration that retired the table could not do this itself. A message's projection and
its serialized blob are both sealed with the vault, and schema upgrades run at startup
*before* unlock, with no key — the same constraint ``app._backfill_sealed_columns`` works
under. So the migration copies the rows into a holding table and this drains it at the
first unlocked boot, where there is a key and where the conversation store's own row shapes
are in reach.

**One transaction per row, and the pen row is deleted inside it.** A half-written thread
would be worse than an unseeded one, and a crash mid-drain must leave every row either
carried over exactly once or still waiting. That also makes the whole thing resumable and
idempotent for free: a row that became a thread is gone from the pen, and a pen that is
empty is dropped, so the second boot does nothing and the third finds no table at all.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from sqlalchemy import Engine, inspect, text
from sqlmodel import Session

from core.db import in_session
from core.vault import Vault
from models._fields import utcnow
from models.conversation import Conversation, Message

# The two halves of "how a message is stored", borrowed rather than restated: the
# projection the listing and the search read, and the blob a cold load rehydrates the tree
# from. Re-deriving either here would be a second answer to a question the store already
# answers, and the one that drifts is the one nobody is looking at.
from services.conversations.projection import _MESSAGE, _project
from services.modes import mode_spec

logger = logging.getLogger(__name__)

#: How long a carried question may be before its title is cut. A research question is often
#: a paragraph and the session list is one line wide.
_TITLE_MAX_CHARS = 80


def title_for(question: str) -> str:
    """A carried thread's name in the session list — the question, folded to one line.

    The question *is* the title, rather than the thread being auto-titled from its first
    exchange like an operator's own: nobody wrote this thread's opening message, so without
    the question in the row the operator meets it with no idea why it exists.

    Lives here because this is now its only caller. It was declared beside the research
    launcher seam while that seam also named threads this way; the launcher is gone, and a
    helper kept where nothing uses it is the next reader's wrong turn.
    """
    question = " ".join(question.split())
    if len(question) <= _TITLE_MAX_CHARS:
        return question
    return question[: _TITLE_MAX_CHARS - 1] + "…"

#: The holding table the migration leaves behind, and the only thing here that knows its
#: name — see ``migrations/versions/b6d20a5c74e1_dropped_the_research_entity.py``.
_CARRYOVER = "research_carryover"

RESEARCH_MODE = "research"

#: Stands in for a report that never arrived. Said plainly, because the alternative is a
#: thread whose only content is a question and no account of why it was never answered.
_UNFINISHED = (
    "This research never finished — it was still {status} when the research pipeline was "
    "retired, so there is no report to carry over. The question is above; ask it again "
    "here and this thread will answer it."
)


async def seed_carried_research(engine: Engine, vault: Vault) -> int:
    """Drain the migration's holding table into real research threads. Returns how many
    threads were seeded.

    Waits for unlock itself, so a caller can fire it at wiring time without knowing when
    the key arrives. Best-effort per row: one unreadable ciphertext (a restored database
    whose keyfile was replaced) must not strand every other report.
    """
    await vault.unlocked_event.wait()
    if not await _has_carryover(engine):
        return 0

    seeded = 0
    for row in await _carried_rows(engine):
        try:
            await _seed_one(engine, vault, row)
        except Exception:
            logger.exception("research: could not carry over %s", row["id"])
            continue
        seeded += 1
    await _drop_when_drained(engine)
    return seeded


async def _has_carryover(engine: Engine) -> bool:
    def work(session: Session) -> bool:
        return inspect(session.get_bind()).has_table(_CARRYOVER)

    return await in_session(engine, work)


async def _carried_rows(engine: Engine) -> list[dict]:
    def work(session: Session) -> list[dict]:
        rows = session.execute(
            text(
                f"SELECT id, owner_id, project_id, question_enc, report_enc, status, "
                f"created_at, finished_at FROM {_CARRYOVER} ORDER BY created_at"
            )
        ).mappings()
        return [dict(row) for row in rows]

    return await in_session(engine, work)


async def _seed_one(engine: Engine, vault: Vault, row: dict) -> None:
    """One research row, as one two-turn thread — question asked, report answered.

    Decrypted outside the write so a bad ciphertext fails before anything is created, and
    re-sealed on the way in: the ciphertext is the same key's, but the *shape* is not (a
    message's blob is a serialized model message, not a bare string), so it has to be
    opened and put back.
    """
    question = vault.decrypt_str(row["question_enc"]).strip()
    report = vault.decrypt_str(row["report_enc"]).strip() if row["report_enc"] else ""
    answer = report or _UNFINISHED.format(status=row["status"])
    asked_at = _as_datetime(row["created_at"]) or utcnow()
    answered_at = _as_datetime(row["finished_at"]) or asked_at

    request = ModelRequest(parts=[UserPromptPart(content=question, timestamp=asked_at)])
    response = ModelResponse(parts=[TextPart(content=answer)], timestamp=answered_at)

    def work(session: Session) -> None:
        conversation = Conversation(
            owner_id=row["owner_id"],
            project_id=row["project_id"],
            mode=RESEARCH_MODE,
            permission_level=mode_spec(RESEARCH_MODE).default_permission,
            title_enc=vault.encrypt_str(title_for(question)),
            # The thread reads as it happened: it is dated when the research was asked
            # for, not when this boot got round to carrying it over.
            created_at=asked_at,
            updated_at=answered_at,
        )
        session.add(conversation)
        session.flush()

        parent_id: str | None = None
        for seq, (message, stamped_at) in enumerate(
            ((request, asked_at), (response, answered_at))
        ):
            kind, projection = _project(message)
            node = Message(
                conversation_id=conversation.id,
                parent_id=parent_id,
                seq=seq,
                kind=kind,
                text=vault.encrypt_str(projection),
                blob=vault.encrypt_str(_MESSAGE.dump_json(message).decode()),
                attachment_ids=[],
                created_at=stamped_at,
            )
            session.add(node)
            session.flush()
            parent_id = node.id
        conversation.active_leaf_id = parent_id
        session.add(conversation)

        # Inside the same transaction as the thread it became, so a crash leaves the row
        # either carried over exactly once or still waiting to be.
        session.execute(text(f"DELETE FROM {_CARRYOVER} WHERE id = :id"), {"id": row["id"]})

    await in_session(engine, work)


async def _drop_when_drained(engine: Engine) -> None:
    """Retire the holding table once nothing is left in it. A row that failed to carry
    over keeps the table alive, so the next boot tries again rather than losing it."""

    def work(session: Session) -> None:
        remaining = session.execute(text(f"SELECT COUNT(*) FROM {_CARRYOVER}")).scalar_one()
        if not remaining:
            session.execute(text(f"DROP TABLE {_CARRYOVER}"))

    await in_session(engine, work)


def _as_datetime(value: object) -> datetime | None:
    """SQLite hands a DateTime column back as a datetime through SQLAlchemy's type, but a
    raw text SELECT bypasses that — so a stored string is parsed here rather than reaching
    a column that will not take it. A stored timestamp is UTC with no zone written on it,
    and the columns it is copied into refuse a naive value, so the zone is put back."""
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return None
