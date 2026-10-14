"""`VAULT-2`: the agent reaching the secrets manager parks for operator approval."""

from __future__ import annotations

from pathlib import Path

from pydantic_ai import ToolApproved, ToolDenied
from pydantic_ai.models.test import TestModel

from agent import ParkedTurn, build_chat_orchestrator, build_resume_orchestrator
from core.container import ServiceContainer
from core.db import init_db, make_engine
from core.vault import Vault
from runs import ApprovalRequired, Run, RunRegistry, RunStatus, ToolCompleted
from services.secret_vault import SecretVaultService
from tools.vault import vault_toolset

OWNER = "operator"
PASSPHRASE = "vault-passphrase"


async def _service(tmp_path: Path) -> SecretVaultService:
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(tmp_path / "keyfile.json")
    await vault.setup("login-password")
    service = SecretVaultService(engine, vault)
    await service.configure(OWNER, PASSPHRASE)
    _ = await service.create(OWNER, name="Production DB", username="admin", password="s3cret")
    return service


async def _run_tool(reg: RunRegistry, service: SecretVaultService | None, tool: str) -> Run:
    orch = build_chat_orchestrator(
        "look up the database credential",
        model=TestModel(custom_output_text="done", call_tools=[f"vault_{tool}"]),
        categories={"vault": vault_toolset()},
        capabilities=ServiceContainer.of(service) if service is not None else ServiceContainer(),
    )
    run = reg.submit(kind="chat", owner_id=OWNER, orchestrator=orch)
    _ = await run.wait()
    return run


async def _resume(
    reg: RunRegistry,
    run: Run,
    service: SecretVaultService | None,
    decision: ToolApproved | ToolDenied,
) -> None:
    """Approve/deny the parked call the way `POST /runs/{id}/approve` does — capabilities
    and all, since a resumed turn re-runs the tool and needs the same handles."""
    parked = run.parked_payload
    assert isinstance(parked, ParkedTurn)
    call_id = parked.requests.approvals[0].tool_call_id
    _ = await reg.resume(
        run.id,
        build_resume_orchestrator(
            parked,
            {call_id: decision},
            capabilities=ServiceContainer.of(service)
            if service is not None
            else ServiceContainer(),
        ),
    )
    _ = await run.wait()


def _types(run: Run) -> list[str]:
    return [e.body.type for e in run.stream.replay()]


def _results(run: Run) -> list[str]:
    return [str(e.body.result) for e in run.stream.replay() if isinstance(e.body, ToolCompleted)]


async def test_every_vault_tool_is_statically_approval_gated():
    # The marking is a property of the toolset itself — no run, no deps, no condition.
    tools = vault_toolset().tools
    assert set(tools) == {"list_entries", "get_entry"}
    assert all(tool.requires_approval for tool in tools.values())


async def test_listing_the_vault_parks_the_run(tmp_path: Path):
    service = await _service(tmp_path)
    run = await _run_tool(RunRegistry(), service, "list_entries")

    assert run.status is RunStatus.awaiting_input
    assert "approval.required" in _types(run)
    assert "tool.completed" not in _types(run)  # requested, never executed

    approval = next(e.body for e in run.stream.replay() if isinstance(e.body, ApprovalRequired))
    assert "list_entries" in approval.name
    # The operator judges the request itself: the model's stated reason rides along.
    assert "reason" in approval.args


async def test_reading_a_credential_parks_and_only_yields_it_once_approved(tmp_path: Path):
    service = await _service(tmp_path)
    reg = RunRegistry()
    run = await _run_tool(reg, service, "get_entry")

    assert run.status is RunStatus.awaiting_input
    await _resume(reg, run, service, ToolApproved())
    assert run.status is RunStatus.done
    # TestModel invents the id, so the read comes back "no such entry" — the point is that
    # the tool body only ever ran on the far side of the approval, and reached the service.
    assert any("No vault entry" in result for result in _results(run))


async def test_a_denied_vault_read_never_touches_the_service(tmp_path: Path):
    service = await _service(tmp_path)
    reg = RunRegistry()
    run = await _run_tool(reg, service, "list_entries")

    await _resume(reg, run, service, ToolDenied(message="no"))
    assert run.status is RunStatus.done
    # Nothing the vault holds reached the model.
    assert all("Production DB" not in result for result in _results(run))


async def test_without_the_capability_the_tools_report_it_absent():
    reg = RunRegistry()
    run = await _run_tool(reg, None, "list_entries")

    await _resume(reg, run, None, ToolApproved())
    assert any("not available" in result for result in _results(run))


async def test_an_approved_read_still_meets_the_vaults_own_lock(tmp_path: Path):
    service = await _service(tmp_path)
    service.lock(OWNER)
    reg = RunRegistry()
    run = await _run_tool(reg, service, "list_entries")

    await _resume(reg, run, service, ToolApproved())
    # Approval is not a key: the operator's lock still stands behind it.
    assert any("locked" in result for result in _results(run))
    assert all("Production DB" not in result for result in _results(run))
