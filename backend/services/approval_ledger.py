"""The conversation's approved calls, as the Auto level's decision model reads them.

Recorded where an approval is settled: by the operator on resume (``routes/runs.py``), and
by an Auto review that cleared a call on its own grounds (``agent/gating.py``). Grants and
structural clearances are not recorded.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import Engine, delete
from sqlmodel import Session, col, select

from core.db import in_session
from core.vault import Vault
from models.approved_call import ApprovedCall
from services.permissions import capability_of
from services.permissions.reviewer import ApprovedBy, PriorApproval

_APPROVED_BY: TypeAdapter[ApprovedBy] = TypeAdapter(ApprovedBy)


@dataclass(frozen=True)
class LedgerEntry:
    tool_call_id: str
    approval: PriorApproval

    @classmethod
    def of(
        cls,
        tool_call_id: str,
        tool_name: str,
        args: Mapping[str, object],
        approved_by: ApprovedBy,
    ) -> LedgerEntry:
        """An entry whose summary is the same line the review row shows for the call."""
        summary = capability_of(tool_name, args).summary
        return cls(tool_call_id, PriorApproval(tool_name, summary, approved_by))


class ApprovalLedger:
    def __init__(self, engine: Engine, vault: Vault) -> None:
        self._engine: Engine = engine
        self._vault: Vault = vault

    async def record(
        self, owner_id: str, conversation_id: str, entries: Sequence[LedgerEntry], *, keep: int
    ) -> None:
        """Append ``entries`` and drop the conversation's rows beyond the latest ``keep``,
        which is all :meth:`recent` is ever asked for."""
        if not entries:
            return
        rows = [
            ApprovedCall(
                owner_id=owner_id,
                conversation_id=conversation_id,
                tool_call_id=entry.tool_call_id,
                tool_name=entry.approval.tool_name,
                summary_enc=self._vault.encrypt_str(entry.approval.summary),
                approved_by=entry.approval.approved_by,
            )
            for entry in entries
        ]

        def work(session: Session) -> None:
            session.add_all(rows)
            session.flush()
            kept = select(ApprovedCall.id).where(
                ApprovedCall.owner_id == owner_id,
                ApprovedCall.conversation_id == conversation_id,
            )
            kept = kept.order_by(
                col(ApprovedCall.created_at).desc(), col(ApprovedCall.id).desc()
            ).limit(max(keep, 0))
            _ = session.exec(
                delete(ApprovedCall)
                .where(col(ApprovedCall.owner_id) == owner_id)
                .where(col(ApprovedCall.conversation_id) == conversation_id)
                .where(col(ApprovedCall.id).not_in(kept.scalar_subquery()))
            )

        await in_session(self._engine, work)

    async def recent(self, owner_id: str, conversation_id: str, limit: int) -> list[PriorApproval]:
        """The conversation's latest ``limit`` approvals, oldest first."""
        if limit <= 0:
            return []

        def work(session: Session) -> list[ApprovedCall]:
            return list(
                session.exec(
                    select(ApprovedCall)
                    .where(ApprovedCall.owner_id == owner_id)
                    .where(ApprovedCall.conversation_id == conversation_id)
                    .order_by(col(ApprovedCall.created_at).desc(), col(ApprovedCall.id).desc())
                    .limit(limit)
                ).all()
            )

        rows = await in_session(self._engine, work)
        return [
            PriorApproval(
                tool_name=row.tool_name,
                summary=self._vault.decrypt_str(row.summary_enc),
                approved_by=_approved_by(row.approved_by),
            )
            for row in reversed(rows)
        ]


def _approved_by(stored: str) -> ApprovedBy:
    """The stored approver, or ``review``, which authorizes nothing, for any word outside
    ``ApprovedBy``."""
    try:
        return _APPROVED_BY.validate_python(stored)
    except ValidationError:
        return "review"
