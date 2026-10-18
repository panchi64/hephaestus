"""The approval ledger: what is recorded, by whom, and what the next review reads back."""

from __future__ import annotations

import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from pydantic_ai import Agent, DeferredToolRequests, ToolApproved, ToolDenied
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.models.test import TestModel
from sqlalchemy import Engine
from sqlmodel import Session, select

import agent.gating as gating
import routes.runs as routes_runs
from agent import ParkedTurn
from core.container import ServiceContainer
from core.db import in_session, init_db, make_engine
from core.vault import Vault
from models.approved_call import ApprovedCall
from routes.deps import app_state
from runs import Orchestrator, Run, RunStatus, RunStream
from services.approval_grants import ApprovalGrantStore
from services.approval_ledger import ApprovalLedger, LedgerEntry
from services.permissions import (
    ApprovedBy,
    PermissionLevel,
    PriorApproval,
    Reviewer,
    ReviewRequest,
    ReviewUnavailable,
    ReviewVerdict,
    ScoredVerdict,
)
from tools import RunDeps

from ._helpers import client_app

OWNER = "operator"
CONV = "conv-1"
KEEP = 20


async def _vault() -> Vault:
    vault = Vault(Path(tempfile.mkdtemp()) / "keyfile.json")
    await vault.setup("pw")
    return vault


async def _ledger(vault: Vault | None = None) -> tuple[ApprovalLedger, Engine]:
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    return ApprovalLedger(engine, vault or await _vault()), engine


def _entry(call_id: str, approved_by: ApprovedBy = "operator") -> LedgerEntry:
    return LedgerEntry.of(call_id, "mail_send", {"to": "a@b.c"}, approved_by)


class TestTheStore:
    async def test_recent_is_scoped_to_the_conversation_and_oldest_first(self):
        ledger, _ = await _ledger()
        await ledger.record(OWNER, CONV, [_entry("c1"), _entry("c2", "review")], keep=KEEP)
        await ledger.record(OWNER, "other", [_entry("c3")], keep=KEEP)
        recent = await ledger.recent(OWNER, CONV, 10)
        assert [p.approved_by for p in recent] == ["operator", "review"]
        assert all(p.tool_name == "mail_send" for p in recent)

    async def test_recent_keeps_only_the_latest(self):
        ledger, _ = await _ledger()
        for n in range(5):
            await ledger.record(OWNER, CONV, [_entry(f"c{n}")], keep=KEEP)
        assert len(await ledger.recent(OWNER, CONV, 2)) == 2
        assert await ledger.recent(OWNER, CONV, 0) == []

    async def test_a_write_prunes_rows_past_what_is_ever_read(self):
        ledger, engine = await _ledger()
        for n in range(5):
            await ledger.record(OWNER, CONV, [_entry(f"c{n}")], keep=3)
        await ledger.record(OWNER, "other", [_entry("x")], keep=3)

        def ids(session: Session) -> list[str]:
            return [
                row.tool_call_id
                for row in session.exec(
                    select(ApprovedCall).where(ApprovedCall.conversation_id == CONV)
                ).all()
            ]

        assert sorted(await in_session(engine, ids)) == ["c2", "c3", "c4"]
        assert len(await ledger.recent(OWNER, "other", 10)) == 1

    async def test_a_word_this_code_no_longer_writes_authorizes_nothing(self):
        vault = await _vault()
        ledger, engine = await _ledger(vault)

        def legacy(session: Session) -> None:
            session.add(
                ApprovedCall(
                    owner_id=OWNER,
                    conversation_id=CONV,
                    tool_call_id="old",
                    tool_name="mail_send",
                    summary_enc=vault.encrypt_str("send"),
                    approved_by="grant",
                )
            )

        await in_session(engine, legacy)
        assert [p.approved_by for p in await ledger.recent(OWNER, CONV, 10)] == ["review"]

    async def test_the_summary_is_stored_sealed(self):
        ledger, engine = await _ledger()
        entry = _entry("c1")
        await ledger.record(OWNER, CONV, [entry], keep=KEEP)

        def rows(session: Session) -> list[ApprovedCall]:
            return list(session.exec(select(ApprovedCall)).all())

        stored = await in_session(engine, rows)
        assert stored[0].summary_enc != entry.approval.summary
        assert (await ledger.recent(OWNER, CONV, 1))[0].summary == entry.approval.summary


async def test_the_rows_go_with_their_conversation():
    async with client_app() as (_client, app):
        state = app_state(app)
        await state.approval_ledger.record(OWNER, CONV, [_entry("c1")], keep=KEEP)
        await state.conversations.delete_conversation(CONV)
        assert await state.approval_ledger.recent(OWNER, CONV, 10) == []


def _call(tool: str, call_id: str, args: dict[str, object] | None = None) -> ToolCallPart:
    return ToolCallPart(tool_name=tool, args=args or {}, tool_call_id=call_id)


def _reviewer(seen: list[ReviewRequest], verdict: ReviewVerdict) -> Reviewer:
    async def reviewer(request: ReviewRequest) -> ScoredVerdict | ReviewUnavailable:
        seen.append(request)
        return ScoredVerdict(verdict, {})

    return reviewer


def _resolving(
    reviewer: Reviewer,
) -> Callable[[ServiceContainer, str], Awaitable[Reviewer | None]]:
    async def resolve(_caps: ServiceContainer, _owner_id: str) -> Reviewer | None:
        return reviewer

    return resolve


async def _settle(
    permission: PermissionLevel, calls: list[ToolCallPart], caps: ServiceContainer
) -> None:
    run = Run(id="r1", kind="chat", owner_id=OWNER, stream=RunStream())
    _ = await gating.settle_deferred(
        run,
        calls,
        caps=caps,
        conversation_id=CONV,
        deps=RunDeps(run=run, owner_id=OWNER, permission=permission),
        messages=[],
        permission=permission,
    )


class TestTheGateRecords:
    async def test_a_review_allow_is_recorded_and_read_by_the_next_review(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        ledger, _ = await _ledger()
        seen: list[ReviewRequest] = []
        allow = ReviewVerdict(risk="high", authorization="explicitly_yes")
        monkeypatch.setattr(gating, "resolve_reviewer", _resolving(_reviewer(seen, allow)))
        caps = ServiceContainer.of(ledger)

        await _settle("auto", [_call("mail_send", "c1", {"to": "a@b.c"})], caps)
        await _settle("auto", [_call("mail_send", "c2", {"to": "a@b.c"})], caps)

        assert list(seen[0].approved_calls) == []
        assert [(p.tool_name, p.approved_by) for p in seen[1].approved_calls] == [
            ("mail_send", "review")
        ]

    async def test_a_structural_clearance_is_not_recorded(self, monkeypatch: pytest.MonkeyPatch):
        ledger, _ = await _ledger()
        monkeypatch.setattr(
            gating, "resolve_reviewer", _resolving(_reviewer([], ReviewVerdict(risk="low")))
        )
        await _settle(
            "auto", [_call("memory_recall", "c1", {"query": "x"})], ServiceContainer.of(ledger)
        )
        assert await ledger.recent(OWNER, CONV, 10) == []

    async def test_a_park_is_not_recorded(self, monkeypatch: pytest.MonkeyPatch):
        ledger, _ = await _ledger()
        monkeypatch.setattr(
            gating, "resolve_reviewer", _resolving(_reviewer([], ReviewVerdict(risk="high")))
        )
        await _settle("auto", [_call("mail_send", "c1")], ServiceContainer.of(ledger))
        assert await ledger.recent(OWNER, CONV, 10) == []

    async def test_a_grant_settled_call_is_not_recorded(self):
        # The live grant reaches the review directly; a ledger copy would outlive a revoke.
        ledger, engine = await _ledger()
        grants = ApprovalGrantStore(engine, 3600)
        _ = await grants.grant(OWNER, CONV, "mail_send")
        await _settle("edit", [_call("mail_send", "c1")], ServiceContainer.of(ledger, grants))
        assert await ledger.recent(OWNER, CONV, 10) == []

    async def test_a_review_that_leant_on_a_grant_is_not_recorded(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        ledger, engine = await _ledger()
        grants = ApprovalGrantStore(engine, 3600)
        _ = await grants.grant(OWNER, CONV, "mail_send", decisive=True)
        neutral = ReviewVerdict(risk="high", authorization="neutral")
        monkeypatch.setattr(gating, "resolve_reviewer", _resolving(_reviewer([], neutral)))
        await _settle("auto", [_call("mail_send", "c1")], ServiceContainer.of(ledger, grants))
        assert await ledger.recent(OWNER, CONV, 10) == []

    async def test_nothing_is_read_when_no_call_needs_a_review(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        ledger, _ = await _ledger()

        async def unexpected(*_args: object, **_kwargs: object) -> list[PriorApproval]:
            raise AssertionError("the ledger was read for a batch with nothing to review")

        monkeypatch.setattr(ledger, "recent", unexpected)
        await _settle("edit", [_call("mail_send", "c1")], ServiceContainer.of(ledger))


async def test_the_operators_approval_is_recorded_on_resume(monkeypatch: pytest.MonkeyPatch):
    def capture(
        _parked: ParkedTurn, _decisions: dict[str, ToolApproved | ToolDenied], **_kwargs: object
    ) -> Orchestrator:
        async def orchestrator(_run: Run) -> None:
            return None

        return orchestrator

    monkeypatch.setattr(routes_runs, "build_resume_orchestrator", capture)
    async with client_app() as (client, app):
        state = app_state(app)
        pending = _call("mail_send", "c1", {"to": "a@b.c"})
        parked = ParkedTurn(
            Agent(TestModel()),
            [],
            DeferredToolRequests(approvals=[pending]),
            settled={},
            conversation_id=CONV,
        )

        async def park(run: Run) -> None:
            run.park(parked)

        run = state.runs.submit(kind="chat", owner_id=OWNER, orchestrator=park)
        await run.wait()
        assert run.status is RunStatus.awaiting_input
        resp = await client.post(
            f"/runs/{run.id}/approve",
            json={"decisions": [{"tool_call_id": "c1", "approved": True}]},
        )
        assert resp.status_code == 202, resp.text
        recorded = await state.approval_ledger.recent(OWNER, CONV, 10)
    assert recorded == [
        PriorApproval("mail_send", recorded[0].summary, "operator"),
    ]
