"""The per-call ``narration``: offered in the schema, gone again before validation.

The argument exists in two places that have to agree — ``tools/describe.py`` puts a
property on every tool's schema, ``tools/narration.py`` takes it back off the raw
arguments before Pydantic AI validates them — and the failure mode if they disagree is not
subtle: the library validates a call against the tool function's own signature with extras
forbidden, so a narration that survives to validation is a ``ValidationError``, which the
library turns into a retry prompt telling the model its call was malformed for using the
property it was offered. The model then writes the same call again.

So the assertions here are end to end, through a real turn: a narrated call runs, the tool
function sees only its own parameters, and the operator's work log keeps the sentence. The
unit assertions underneath cover the two shapes providers send arguments in, and the
second pass an approval resume performs over arguments this capability has already seen.
"""

from __future__ import annotations

import json

from pydantic import BaseModel
from pydantic_ai import FunctionToolset, RunContext, ToolApproved, ToolDefinition
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from agent import ParkedTurn, build_chat_orchestrator, build_resume_orchestrator
from runs import (
    ApprovalRequired,
    Run,
    RunRegistry,
    RunStream,
    ToolCompleted,
    ToolStarted,
)
from services.tool_sensitivity import SENSITIVITY_METADATA_KEY, Sensitivity
from tools import NarrationCapability, RunDeps, build_agent_toolsets, strip_narration
from tools.describe import NARRATION_ARG, NARRATION_TEXT

#: What the model writes when it calls the tool below — the thing the operator is meant to
#: read in the work log, and the thing the tool function must never be handed.
WHY = "Checking whether the migration already ran before I add another."

_TOOL_DEF = ToolDefinition(name="danger_delete_thing")


def _recording_categories(seen: list[dict[str, object]]):
    """One acting tool that writes down every keyword argument it was called with.

    It declares itself a workspace write, which is inside the ceiling a thread's default
    level permits, so the call runs instead of parking for an approval that has nothing to
    do with the argument under test. (The narration it is offered like every other tool.)
    """
    toolset: FunctionToolset[RunDeps] = FunctionToolset()

    @toolset.tool_plain(metadata={SENSITIVITY_METADATA_KEY: Sensitivity.WORKSPACE_WRITE})
    def delete_thing(name: str) -> str:
        seen.append({"name": name})
        return f"deleted {name}"

    return {"danger": toolset}


def _narrating_model() -> FunctionModel:
    """A model that calls the tool once, with a narration beside its real argument, and
    then answers. The narration is written into the JSON the provider would send, so the
    call travels the same path a real one does rather than being injected past validation.
    """
    calls: list[AgentInfo] = []

    async def stream_fn(_messages, info: AgentInfo):
        calls.append(info)
        if len(calls) == 1:
            yield {
                0: DeltaToolCall(
                    name="danger_delete_thing",
                    json_args=json.dumps({"name": "widget", NARRATION_ARG: WHY}),
                )
            }
        else:
            yield "done"

    return FunctionModel(stream_function=stream_fn)


def _events[B: BaseModel](run: Run, kind: type[B]) -> list[B]:
    return [e.body for e in run.stream.replay() if isinstance(e.body, kind)]


async def _context() -> RunContext[RunDeps]:
    run = Run(id="t", kind="chat", owner_id="operator", stream=RunStream())
    return RunContext(
        deps=RunDeps(run=run, owner_id="operator"), model=TestModel(), usage=RunUsage()
    )


async def _stripped(args):
    ctx = await _context()
    return await NarrationCapability().before_tool_validate(
        ctx,
        call=ToolCallPart(tool_name="danger_delete_thing", args=args, tool_call_id="c"),
        tool_def=_TOOL_DEF,
        args=args,
    )


# --- a real turn ----------------------------------------------------------------------


async def test_a_narrated_call_runs_and_the_tool_never_sees_the_narration():
    seen: list[dict[str, object]] = []
    reg = RunRegistry()
    orch = build_chat_orchestrator(
        "delete the widget", model=_narrating_model(), categories=_recording_categories(seen)
    )
    run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch)
    await run.wait()

    # A retry prompt is the specific failure this whole seam exists to prevent, so it is
    # named rather than left to show up as an opaque "the tool ran twice".
    assert seen == [{"name": "widget"}], (
        "the tool function was handed something other than its own parameters — the "
        "narration reached validation and the model was sent round a retry"
    )
    assert [e.type for e in _events(run, ToolCompleted)] == ["tool.completed"]


async def test_the_work_log_keeps_the_sentence_the_tool_never_got():
    """The whole point of the argument. Nothing in the wire protocol had to change for
    this: the call frame is built from the raw ``ToolCallPart``, before the capability
    touches anything, and that same part is what a cold history read replays."""
    reg = RunRegistry()
    orch = build_chat_orchestrator(
        "delete the widget", model=_narrating_model(), categories=_recording_categories([])
    )
    run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch)
    await run.wait()

    started = _events(run, ToolStarted)
    assert [body.args.get(NARRATION_ARG) for body in started] == [WHY]


# --- the capability -------------------------------------------------------------------


async def test_a_dict_payload_loses_the_narration_and_nothing_else():
    assert await _stripped({"name": "widget", NARRATION_ARG: WHY}) == {"name": "widget"}


async def test_a_json_payload_loses_the_narration_and_nothing_else():
    stripped = await _stripped(json.dumps({"name": "widget", NARRATION_ARG: WHY}))
    assert isinstance(stripped, str)
    assert json.loads(stripped) == {"name": "widget"}


async def test_the_second_pass_an_approval_resume_makes_is_a_no_op():
    """A parked call is validated again when the operator approves it, through this same
    hook — so the capability always sees arguments it has already stripped once, and a
    version that assumed otherwise would be correct here only by accident."""
    for payload in ({"name": "widget", NARRATION_ARG: WHY}, {"name": "widget"}):
        once = await _stripped(payload)
        assert await _stripped(once) == once
    for payload in (
        json.dumps({"name": "widget", NARRATION_ARG: WHY}),
        json.dumps({"name": "widget"}),
    ):
        once = await _stripped(payload)
        assert await _stripped(once) == once


async def test_an_untouched_payload_comes_back_byte_identical():
    # Not merely equal: re-serialising a payload nothing needed doing to would hand the
    # validator different bytes from the ones the model sent, for no reason at all.
    payload = '{"name":   "widget"}'
    assert await _stripped(payload) is payload


async def test_arguments_this_capability_cannot_read_are_passed_straight_through():
    # The validator has a far better account of what is wrong with a truncated payload
    # than a stripper does; rewriting it here would replace a precise error with a vague one.
    for payload in ('{"name": "wid', "not json at all", "[1, 2, 3]"):
        assert await _stripped(payload) == payload


async def test_a_tool_that_declares_its_own_narration_keeps_it():
    """Every tool is offered the property now, including tools this repo does not write —
    an MCP server's, an integration's. One that already takes a ``narration`` must get it:
    stripping it would hand the tool a call missing an argument it asked for."""
    own = {"type": "string", "description": "The caption to attach."}
    tool_def = ToolDefinition(
        name="external_annotate",
        parameters_json_schema={"type": "object", "properties": {NARRATION_ARG: own}},
    )
    payload = {NARRATION_ARG: "A caption"}
    ctx = await _context()
    kept = await NarrationCapability().before_tool_validate(
        ctx,
        call=ToolCallPart(tool_name="external_annotate", args=payload, tool_call_id="c"),
        tool_def=tool_def,
        args=payload,
    )
    assert kept == payload


async def test_the_describing_stage_leaves_a_declared_narration_as_the_tool_wrote_it():
    toolset: FunctionToolset[RunDeps] = FunctionToolset()

    @toolset.tool_plain
    def annotate(narration: str) -> str:
        """Attach a caption.

        Args:
            narration: The caption to attach.
        """
        return narration

    offered = await build_agent_toolsets({"ext": toolset})[0].get_tools(await _context())
    schema = offered["ext_annotate"].tool_def.parameters_json_schema
    assert schema["properties"][NARRATION_ARG]["description"] != NARRATION_TEXT
    assert NARRATION_ARG in schema.get("required", [])


def test_strip_narration_gives_the_loop_guard_the_question_without_the_prose():
    # `agent/translate.py` fingerprints a repeating call by its arguments. Two spellings of
    # the same reason for the same call are the same call, so the sentence is left out of
    # the signature — otherwise every repeat looks novel and the guard stops guarding.
    assert strip_narration({"name": "widget", NARRATION_ARG: WHY}) == strip_narration(
        {"name": "widget", NARRATION_ARG: "Trying once more."}
    )


# --- and the park/resume path it has to survive ----------------------------------------


async def test_a_narrated_call_survives_being_parked_and_approved():
    """The resume path is where idempotence stops being theoretical: the approval carries
    the parked call's own arguments back into validation, so the hook runs over them a
    second time before the tool body is finally reached."""
    seen: list[dict[str, object]] = []
    categories = _recording_categories(seen)
    gated: FunctionToolset[RunDeps] = FunctionToolset()

    @gated.tool_plain(requires_approval=True)
    def delete_thing(name: str) -> str:
        seen.append({"name": name})
        return f"deleted {name}"

    categories["danger"] = gated

    reg = RunRegistry()
    orch = build_chat_orchestrator(
        "delete the widget", model=_narrating_model(), categories=categories
    )
    run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch)
    await run.wait()

    parked = run.parked_payload
    assert isinstance(parked, ParkedTurn)
    call_id = parked.requests.approvals[0].tool_call_id
    # The operator is shown the model's reason alongside the arguments they are ruling on.
    approval = _events(run, ApprovalRequired)[0]
    assert approval.args.get(NARRATION_ARG) == WHY

    await reg.resume(run.id, build_resume_orchestrator(parked, {call_id: ToolApproved()}))
    await run.wait()

    assert seen == [{"name": "widget"}]
