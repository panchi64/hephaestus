"""The conversation's approved calls, as the Auto level's decision model reads them.

Recorded where an approval is settled: by the operator on resume (``routes/runs.py``), and
by a grant or an Auto review at the deferred split (``agent/gating.py``). Calls the
structural judge clears are not recorded.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import cast

from sqlalchemy import Engine
from sqlmodel import Session, col, select

from core.db import in_session
from core.vault import Vault
from models.approved_call import ApprovedCall
from services.permissions import capability_of
from services.permissions.reviewer import ApprovedBy, PriorApproval

_APPROVERS: frozenset[str] = frozenset({"operator", "grant", "review"})


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
        self, owner_id: str, conversation_id: str, entries: Sequence[LedgerEntry]
    ) -> None:
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
                # An unknown word reads as the weakest approver, which authorizes nothing.
                approved_by=cast(
                    "ApprovedBy", row.approved_by if row.approved_by in _APPROVERS else "review"
                ),
            )
            for row in reversed(rows)
        ]
