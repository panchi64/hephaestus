"""Plan mode — the written plan, and the level transitions either side of it.

The Plan permission level has always enforced read-only by *withholding* every mutating
tool (``services/tool_policy.permission_disabled_tools``). What it lacked was an ending: a
Plan turn could work everything out and had nowhere to put it, and no way for the operator
to say yes. This is that ending.

**One service for the document and the level, on purpose.** Approving a plan is not two
independent facts — it records the operator's yes *and* raises the thread to a level that
can act on it — and a caller able to do one without the other is a caller that can raise a
thread's permissions with no approved plan behind it. Keeping them in one method means
there is no such caller.

**It holds ``ConversationStore`` so no tool has to.** Setting the level is a write to the
conversation row, and the agent's capability bag deliberately does not contain the
conversation store: a tool that could reach every thread the operator has is not a
capability anything here needs. This is the narrow seam instead — two level transitions,
both about the thread the run is already in.

**Seeding the task list is part of approving.** An approved plan's steps *are* the work, so
execution begins from a list that already says so. It goes through the same
``ConversationTasks`` the agent's own tools write through, so the panel cannot tell a seeded
list from a written one — which is right: by then it is simply the thread's task list.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TypedDict

from sqlalchemy import Engine
from sqlmodel import Session, select

from core.db import in_session
from core.serde import parse_json
from core.vault import Vault, VaultError, VaultLocked
from models._fields import new_id, utcnow
from models.plan import ConversationPlan
from runs import PermissionChanged, PlanUpdated, Run
from services.conversations import ConversationStore
from services.task_list import ConversationTasks

logger = logging.getLogger(__name__)

#: The tool whose approval is a plan being accepted. Named here rather than in ``tools/``
#: because the approve route has to recognise it (``routes/runs.py``) and ``routes`` must
#: not import a toolset to do so.
PLAN_SUBMIT_TOOL = "plan_submit"

#: The level a thread drops to on entering plan mode, and the one an approved plan raises it
#: to. Spelled here rather than at the two call sites so the pair is read as the round trip
#: it is.
PLANNING_LEVEL = "plan"
ACTING_LEVEL = "auto"


class PlanPayload(TypedDict):
    title: str
    body: str
    steps: list[str]
    status: str
    revision: int


@dataclass(frozen=True, slots=True)
class Plan:
    """A thread's plan as everything above this layer reads it."""

    title: str
    body: str
    steps: list[str]
    status: str
    revision: int

    def payload(self) -> PlanPayload:
        """The wire shape — identical on the event and on the REST backfill, so a reload
        rebuilds exactly what the stream was drawing."""
        return {
            "title": self.title,
            "body": self.body,
            "steps": list(self.steps),
            "status": self.status,
            "revision": self.revision,
        }


class PlanMode:
    """The plan document for a conversation, and the two level moves around it."""

    def __init__(
        self,
        db_engine: Engine,
        vault: Vault,
        *,
        conversations: ConversationStore,
        tasks: ConversationTasks,
    ) -> None:
        self._db: Engine = db_engine
        self._vault: Vault = vault
        self._conversations: ConversationStore = conversations
        self._tasks: ConversationTasks = tasks

    # ── the document ────────────────────────────────────────────────────────────────

    async def current(self, owner_id: str, conversation_id: str) -> Plan | None:
        """This thread's plan, or None where there is none — including where the vault is
        locked, for the same reason the task list reads as empty then: a surface that
        cannot be drawn correctly should be drawn empty rather than wrongly."""

        def work(session: Session) -> tuple[str, str, int] | None:
            row = session.exec(
                select(ConversationPlan)
                .where(ConversationPlan.owner_id == owner_id)
                .where(ConversationPlan.conversation_id == conversation_id)
            ).first()
            # Read out inside the session: a detached row's attributes are not ours to
            # reach for once it has closed.
            return (row.plan_enc, row.status, row.revision) if row else None

        stored = await in_session(self._db, work)
        if stored is None:
            return None
        sealed, status, revision = stored
        try:
            document = parse_json(self._vault.decrypt_str(sealed))
        except VaultLocked, VaultError:
            logger.debug("plan unreadable for %s: vault locked", conversation_id)
            return None
        if not isinstance(document, dict):
            raise ValueError("a stored plan is not an object")
        steps = document.get("steps", [])
        return Plan(
            title=str(document.get("title", "")),
            body=str(document.get("body", "")),
            steps=[str(step) for step in steps] if isinstance(steps, list) else [],
            status=status,
            revision=revision,
        )

    async def submit(
        self,
        owner_id: str,
        conversation_id: str,
        *,
        title: str,
        body: str,
        steps: list[str],
        run: Run | None = None,
    ) -> Plan | None:
        """Record a plan awaiting the operator's answer, announcing it.

        Written *before* the turn parks rather than after it is approved, so the panel has
        the document to render while the operator is deciding — and so a reload mid-decision
        finds it. ``None`` when the vault was locked and nothing was stored: the caller
        needs the distinction, because a park announced against a plan that was never
        written would leave the operator deciding about a document a reload cannot show.
        """
        try:
            sealed = self._vault.encrypt_str(
                json.dumps({"title": title, "body": body, "steps": list(steps)})
            )
        except VaultLocked, VaultError:
            logger.debug("plan not stored for %s: vault locked", conversation_id)
            return None

        def work(session: Session) -> int:
            row = session.exec(
                select(ConversationPlan)
                .where(ConversationPlan.owner_id == owner_id)
                .where(ConversationPlan.conversation_id == conversation_id)
            ).first()
            if row is None:
                row = ConversationPlan(
                    id=new_id(),
                    owner_id=owner_id,
                    conversation_id=conversation_id,
                    plan_enc=sealed,
                )
            else:
                row.plan_enc = sealed
                # A resubmission answering feedback is a new revision, which is what lets
                # the panel treat it as a fresh arrival rather than a redraw.
                row.revision += 1
            row.status = "pending"
            row.updated_at = utcnow()
            session.add(row)
            session.flush()
            return row.revision

        revision = await in_session(self._db, work)
        plan = Plan(title=title, body=body, steps=list(steps), status="pending", revision=revision)
        self._announce(plan, run)
        return plan

    async def settle(
        self, owner_id: str, conversation_id: str, status: str, *, run: Run | None = None
    ) -> Plan | None:
        """Move a plan to ``status`` without touching the document or the thread's level.

        What a denial and a revision request both do. The level is deliberately not a
        parameter: only approval moves it, and it moves it through :meth:`approve`.
        """
        await self._set_status(owner_id, conversation_id, status)
        plan = await self.current(owner_id, conversation_id)
        if plan is not None:
            self._announce(plan, run)
        return plan

    async def delete_for_conversation(self, owner_id: str, conversation_id: str) -> None:
        """Drop a thread's plan when the thread goes. Works while the vault is locked: it
        only destroys."""

        def work(session: Session) -> None:
            for row in session.exec(
                select(ConversationPlan)
                .where(ConversationPlan.owner_id == owner_id)
                .where(ConversationPlan.conversation_id == conversation_id)
            ).all():
                session.delete(row)

        await in_session(self._db, work)

    # ── the level ───────────────────────────────────────────────────────────────────

    async def enter(self, conversation_id: str, *, reason: str, run: Run | None = None) -> str:
        """Put the thread into plan mode, announcing the new level.

        Only ever narrows what the thread may do, which is why nothing approves it.
        """
        stored = await self._conversations.set_permission_level(conversation_id, PLANNING_LEVEL)
        if run is not None:
            _ = run.emit(PermissionChanged(level=stored, reason=reason))
        return stored

    async def approve(
        self, owner_id: str, conversation_id: str, *, run: Run | None = None
    ) -> tuple[Plan | None, str]:
        """Accept the pending plan: record the yes, seed the tasks, raise the level.

        Returns the approved plan and the level the thread is now at. The order matters
        only in one respect — the level is raised **last**, so a failure anywhere above it
        leaves a thread that cannot act rather than one that can with nothing agreed.
        """
        plan = await self.current(owner_id, conversation_id)
        if plan is None:
            # Nothing was stored (a locked vault at submit time). The operator approved
            # something, so the level still moves — refusing here would strand a thread the
            # operator has said yes to, and the plan they read is in the transcript either
            # way.
            logger.info("approving a plan with no stored document for %s", conversation_id)
        else:
            await self._set_status(owner_id, conversation_id, "approved")
            plan = Plan(
                title=plan.title,
                body=plan.body,
                steps=plan.steps,
                status="approved",
                revision=plan.revision,
            )
            self._announce(plan, run)
            if plan.steps:
                _ = await self._tasks.seed(owner_id, conversation_id, plan.steps, run=run)
        level = await self._conversations.set_permission_level(conversation_id, ACTING_LEVEL)
        if run is not None:
            _ = run.emit(PermissionChanged(level=level, reason="plan approved"))
        return plan, level

    # ── internals ───────────────────────────────────────────────────────────────────

    def _announce(self, plan: Plan, run: Run | None) -> None:
        if run is not None:
            _ = run.emit(PlanUpdated(**plan.payload()))

    async def _set_status(self, owner_id: str, conversation_id: str, status: str) -> None:
        def work(session: Session) -> None:
            row = session.exec(
                select(ConversationPlan)
                .where(ConversationPlan.owner_id == owner_id)
                .where(ConversationPlan.conversation_id == conversation_id)
            ).first()
            if row is None:
                return
            row.status = status
            row.updated_at = utcnow()
            session.add(row)

        await in_session(self._db, work)
