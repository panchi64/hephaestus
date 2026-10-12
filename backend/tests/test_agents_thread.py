"""Running an agent as a thread's own."""

from __future__ import annotations

from agent.injections import contributor_id
from runs import Run, RunStream
from services.agents import DEFAULT_AGENT, AgentCatalog, AgentDefinition, ToolAccess
from services.agents.thread import DELEGATION_TOOLS, as_thread, resolve_thread_agent
from services.conversations import ConversationBinding
from tools.deps import RunDeps
from tools.plan import _retarget

from ._agent_helpers import OWNER, agent_values, make_stores


def _agent(**more) -> AgentDefinition:
    return AgentDefinition(name="a", description="An agent.", **more)


def test_the_ceiling_lowers_the_threads_level():
    applied = as_thread(_agent(permission_ceiling="plan"), ConversationBinding(permission="auto"))
    assert applied.binding.permission == "plan"
    assert applied.binding.permission_ceiling == "plan"


def test_the_ceiling_never_raises_the_threads_level():
    applied = as_thread(_agent(permission_ceiling="auto"), ConversationBinding(permission="plan"))
    assert applied.binding.permission == "plan"


def test_no_ceiling_leaves_the_level_alone():
    applied = as_thread(_agent(), ConversationBinding(permission="auto"))
    assert applied.binding.permission == "auto"
    assert applied.binding.permission_ceiling is None


def test_its_tools_land_on_the_binding():
    tools = ToolAccess(allow=frozenset({"files"}))
    assert as_thread(_agent(tools=tools), ConversationBinding()).binding.tools == tools


def test_delegation_is_withheld_unless_the_agent_may_delegate():
    assert as_thread(_agent(), ConversationBinding()).withheld == DELEGATION_TOOLS
    assert as_thread(_agent(can_delegate=True), ConversationBinding()).withheld == frozenset()


def test_a_brief_provider_exists_only_with_instructions():
    assert as_thread(_agent(), ConversationBinding()).instructions == ()
    (provider,) = as_thread(_agent(instructions="Be brief."), ConversationBinding()).instructions
    assert contributor_id(provider) == "agent_brief"


async def test_the_brief_provider_returns_the_instructions():
    (provider,) = as_thread(_agent(instructions="Be brief."), ConversationBinding()).instructions
    assert await provider(None) == "Be brief."


async def test_a_thread_resolves_its_named_agent(tmp_path):
    store, _ = await make_stores(tmp_path)
    await store.create(OWNER, agent_values("live", permission_ceiling="plan"), published=True)
    catalog = AgentCatalog(store)

    named = await resolve_thread_agent(
        catalog, OWNER, ConversationBinding(agent_name="live", permission="auto"), None
    )
    assert named.agent.name == "live"
    assert named.binding.permission == "plan"

    unknown = await resolve_thread_agent(
        catalog, OWNER, ConversationBinding(agent_name="gone"), None
    )
    assert unknown.agent.name == DEFAULT_AGENT


def _ctx(permission: str, ceiling: str | None):
    run = Run(id="r", kind="chat", owner_id=OWNER, stream=RunStream())
    deps = RunDeps(run=run, owner_id=OWNER, permission=permission, permission_ceiling=ceiling)
    return type("Ctx", (), {"deps": deps})()


def test_approving_a_plan_never_lifts_a_run_past_its_agents_ceiling():
    ctx = _ctx("plan", "edit")
    _retarget(ctx, "auto")
    assert ctx.deps.permission == "edit"


def test_without_a_ceiling_approving_a_plan_widens_the_level():
    ctx = _ctx("plan", None)
    _retarget(ctx, "auto")
    assert ctx.deps.permission == "auto"
