"""`run_code`: the tools a thread's level clears unasked, callable from one script.

Covers what a script may call (read off the composed stack at the live level) and what
happens to a call it makes (ruled on inline, or refused with a pointer to call it directly).
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Mapping

from pydantic import JsonValue, TypeAdapter
from pydantic_ai import (
    AgentRunResult,
    ApprovalRequired,
    CallDeferred,
    DeferredToolRequests,
    FunctionToolset,
    RunContext,
)
from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart
from pydantic_ai.models import Model
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.toolsets.abstract import AbstractToolset, ToolsetTool
from pydantic_ai.usage import RunUsage

from agent import stream_agent_run
from agent.code_mode import (
    NATIVE_ONLY,
    CodeModeLimits,
    code_mode_capability,
    code_mode_limits,
)
from agent.factory import build_agent
from core.config import Settings
from runs import Run, RunStream, ToolCompleted, ToolFailed, ToolProgress, ToolStarted
from services.conversation_view import ToolView, project_tree
from services.permissions import PermissionLevel, blocked_message
from tools import RunDeps, build_agent_toolsets, core_categories
from tools.code_mode_marks import RUN_CODE

from ._json import s

_METADATA = TypeAdapter(dict[str, JsonValue])
_JSON = TypeAdapter(JsonValue)


def _result_of(body: ToolCompleted | ToolView) -> JsonValue:
    return _JSON.validate_python(vars(body)["result"])


def _run() -> Run:
    return Run(id="t", kind="chat", owner_id="operator", stream=RunStream())


def _probe_category() -> FunctionToolset[RunDeps]:
    """One tool per sensitivity class the levels split on."""
    toolset: FunctionToolset[RunDeps] = FunctionToolset()

    @toolset.tool_plain(metadata={"sensitivity": "read"})
    def look(name: str) -> str:
        return f"saw {name}"

    @toolset.tool_plain(metadata={"sensitivity": "workspace_write"})
    def jot(text: str) -> str:
        return f"wrote {text}"

    @toolset.tool_plain(metadata={"sensitivity": "host_exec"})
    def launch(what: str) -> str:
        return f"ran {what}"

    return toolset


def _wrapped(categories: Mapping[str, AbstractToolset[RunDeps]]) -> AbstractToolset[RunDeps]:
    stack = build_agent_toolsets(categories)[0]
    wrapper = code_mode_capability().get_wrapper_toolset(stack)
    assert wrapper is not None
    return wrapper


_SIGNATURE = re.compile(r"^(?:async )?def (\w+)\(", re.MULTILINE)


def _callable(tools: dict[str, ToolsetTool[RunDeps]]) -> set[str]:
    """What a script may call: the functions ``run_code``'s description declares."""
    return set(_SIGNATURE.findall(tools[RUN_CODE].tool_def.description or ""))


async def _script_catalog(permission: PermissionLevel) -> tuple[set[str], set[str]]:
    """``(what a script may call, what the model is offered directly)`` at this level."""
    categories = {**core_categories(), "x": _probe_category()}
    deps = RunDeps(run=_run(), owner_id="operator", permission=permission)
    ctx = RunContext(deps=deps, model=TestModel(), usage=RunUsage())
    tools = await _wrapped(categories).get_tools(ctx)
    return set(_callable(tools)), set(tools) - {RUN_CODE}


class TestWhatAScriptMayCall:
    async def test_manual_folds_only_the_reads(self):
        folded, _ = await _script_catalog("manual")
        assert {"x_look", "files_read_file", "builtin_now"} <= folded
        assert not {"x_jot", "x_launch", "files_write_file"} & folded

    async def test_edit_and_auto_fold_the_workspace_writes_too(self):
        for level in ("edit", "auto"):
            folded, _ = await _script_catalog(level)
            assert {"x_look", "x_jot", "files_write_file"} <= folded, level
            assert "x_launch" not in folded, level

    async def test_yolo_folds_what_it_clears(self):
        folded, _ = await _script_catalog("yolo")
        assert {"x_look", "x_jot", "x_launch"} <= folded

    async def test_the_turn_shaping_tools_never_fold(self):
        for level in ("manual", "auto", "yolo"):
            folded, direct = await _script_catalog(level)
            assert not NATIVE_ONLY & folded, level
            assert "plan_enter" in direct, level

    async def test_the_code_runners_stay_direct(self):
        folded, direct = await _script_catalog("yolo")
        assert "shell_run_command" in direct and "shell_run_command" not in folded
        assert "shell_start_command" not in folded
        assert "code_execute" in direct and "code_execute" not in folded

    async def test_a_folded_tool_is_still_a_direct_call(self):
        folded, direct = await _script_catalog("auto")
        assert folded <= direct

    async def test_a_level_change_moves_the_catalog_at_the_next_read(self):
        """`CodeMode` re-reads the catalog every step, off the live `deps.permission`."""
        categories = {**core_categories(), "x": _probe_category()}
        stack = _wrapped(categories)
        deps = RunDeps(run=_run(), owner_id="operator", permission="auto")
        ctx = RunContext(deps=deps, model=TestModel(), usage=RunUsage())
        assert "x_jot" in _callable(await stack.get_tools(ctx))
        deps.permission = "manual"
        assert "x_jot" not in _callable(await stack.get_tools(ctx))

    async def test_run_code_is_described_as_a_read_with_a_narration(self):
        categories = {**core_categories(), "x": _probe_category()}
        stack = _wrapped(categories)
        deps = RunDeps(run=_run(), owner_id="operator", permission="auto")
        tool_def = (
            await stack.get_tools(RunContext(deps=deps, model=TestModel(), usage=RunUsage()))
        )[RUN_CODE].tool_def
        assert tool_def.metadata is not None
        assert tool_def.metadata["sensitivity"] == "read"
        assert "narration" in tool_def.parameters_json_schema["properties"]
        description = tool_def.description
        assert description is not None
        assert "async def x_look(" in description
        assert "narration" not in description.split("```python", 1)[1]


class TestTheLimits:
    def test_the_call_cap_is_the_scripts_own(self):
        assert code_mode_limits(Settings()).max_tool_calls == 25
        assert code_mode_limits(Settings(code_mode_max_tool_calls=10)).max_tool_calls == 10


def _script_then_answer(code: str, *, direct: tuple[str, dict[str, str]] | None = None):
    """A model that sends one call — a script, or ``direct`` — then answers."""
    seen: list[list[ModelMessage]] = []

    async def stream_fn(
        messages: list[ModelMessage], _info: AgentInfo
    ) -> AsyncIterator[str | dict[int, DeltaToolCall]]:
        seen.append(messages)
        if len(seen) == 1:
            if direct is not None:
                yield {0: DeltaToolCall(name=direct[0], json_args=json.dumps(direct[1]))}
            else:
                yield {
                    0: DeltaToolCall(
                        name=RUN_CODE, json_args=json.dumps({"code": code}), tool_call_id="s"
                    )
                }
        else:
            yield "done"

    return FunctionModel(stream_function=stream_fn), seen


def _gated_categories(calls: list[str]) -> dict[str, AbstractToolset[RunDeps]]:
    """A catalog read (`corpus_retrieve`) and an unclassified tool; both raise ApprovalRequired
    until approved."""
    corpus: FunctionToolset[RunDeps] = FunctionToolset()

    @corpus.tool
    async def retrieve(ctx: RunContext[RunDeps], query: str) -> str:
        if not ctx.tool_call_approved:
            raise ApprovalRequired()
        calls.append("retrieve")
        return f"passages for {query}"

    other: FunctionToolset[RunDeps] = FunctionToolset()

    # Declared a read so the level folds it, but absent from the shipped catalog, so Auto
    # won't clear it unreviewed.
    @other.tool(metadata={"sensitivity": "read"})
    async def recall(ctx: RunContext[RunDeps], query: str) -> str:
        _ = query
        if not ctx.tool_call_approved:
            raise ApprovalRequired()
        calls.append("recall")
        return "secret"

    @other.tool_plain(metadata={"sensitivity": "read"})
    def look(name: str) -> str:
        calls.append(f"look:{name}")
        return f"saw {name}"

    @other.tool_plain(metadata={"sensitivity": "workspace_write"})
    def jot(text: str) -> str:
        calls.append(f"jot:{text}")
        return f"wrote {text}"

    return {"corpus": corpus, "x": other}


async def _drive(
    model: Model,
    categories: Mapping[str, AbstractToolset[RunDeps]],
    *,
    permission: PermissionLevel = "auto",
    limits: CodeModeLimits | None = None,
) -> tuple[Run, AgentRunResult[DeferredToolRequests | str], RunDeps]:
    agent = build_agent(model, categories=categories, code_mode=limits)
    run = _run()
    deps = RunDeps(run=run, owner_id="operator", permission=permission)
    async with agent.iter("go", deps=deps) as agent_run:
        await stream_agent_run(agent_run, run)
    assert agent_run.result is not None
    return run, agent_run.result, deps


def _frames(run: Run) -> list[ToolStarted | ToolCompleted | ToolFailed]:
    bodies = [e.body for e in run.stream.replay() if e.body.type.startswith("tool.")]
    assert not any(isinstance(b, ToolProgress) for b in bodies)
    return [b for b in bodies if isinstance(b, ToolStarted | ToolCompleted | ToolFailed)]


def _script_result(run: Run) -> ToolCompleted:
    return next(f for f in _frames(run) if isinstance(f, ToolCompleted) and f.tool_call_id == "s")


class TestAScriptsCalls:
    async def test_a_self_gated_read_clears_inline_at_auto(self):
        calls: list[str] = []
        model, _ = _script_then_answer('r = await corpus_retrieve(query="q")\nr')
        run, result, _ = await _drive(model, _gated_categories(calls))

        assert calls == ["retrieve"]
        assert result.output == "done"
        nested = [f for f in _frames(run) if f.parent_tool_call_id == "s"]
        assert [f.type for f in nested] == ["tool.started", "tool.completed"]
        completed = nested[1]
        assert isinstance(completed, ToolCompleted)
        assert _result_of(completed) == "passages for q"
        script = _script_result(run)
        assert _result_of(script) == "passages for q"
        order = [(f.type, f.tool_call_id) for f in _frames(run)]
        assert order == [
            ("tool.started", "s"),
            ("tool.started", "s__1"),
            ("tool.completed", "s__1"),
            ("tool.completed", "s"),
        ]
        reviewed = [e.body for e in run.stream.replay() if e.body.type == "review.completed"]
        assert [r.tool_call_id for r in reviewed] == ["s__1"]

    async def test_one_that_needs_the_operator_is_refused_with_the_way_round(self):
        calls: list[str] = []
        code = (
            'out = ""\n'
            "try:\n"
            '    await x_recall(query="q")\n'
            "except Exception as e:\n"
            "    out = str(e)\n"
            "out"
        )
        model, _ = _script_then_answer(code)
        run, result, _ = await _drive(model, _gated_categories(calls))

        assert calls == []
        assert not isinstance(result.output, DeferredToolRequests)
        failed = next(f for f in _frames(run) if isinstance(f, ToolFailed))
        assert failed.parent_tool_call_id == "s"
        assert "directly, outside `run_code`" in failed.error
        script = _script_result(run)
        assert "directly, outside `run_code`" in s(_result_of(script))

    async def test_the_same_call_made_directly_still_parks(self):
        calls: list[str] = []
        model, _ = _script_then_answer("", direct=("x_recall", {"query": "q"}))
        _, result, _ = await _drive(model, _gated_categories(calls))

        assert isinstance(result.output, DeferredToolRequests)
        assert [c.tool_name for c in result.output.approvals] == ["x_recall"]
        assert calls == []

    @staticmethod
    async def _narrowed_to(level: PermissionLevel) -> tuple[list[str], Run]:
        """A script that narrows the thread to ``level``, then calls a write its earlier
        catalog offers."""
        calls: list[str] = []
        categories = _gated_categories(calls)
        narrow: FunctionToolset[RunDeps] = FunctionToolset()

        @narrow.tool(metadata={"sensitivity": "read"})
        async def down(ctx: RunContext[RunDeps]) -> str:
            ctx.deps.permission = level
            return "narrowed"

        categories["n"] = narrow
        code = (
            "await n_down()\n"
            'out = ""\n'
            "try:\n"
            '    await x_jot(text="a")\n'
            "except Exception as e:\n"
            "    out = str(e)\n"
            "out"
        )
        model, _ = _script_then_answer(code)
        run, _, _ = await _drive(model, categories)
        return calls, run

    async def test_a_level_that_moved_under_the_script_refuses_what_it_no_longer_clears(self):
        calls, run = await self._narrowed_to("manual")

        assert calls == []
        failed = next(f for f in _frames(run) if isinstance(f, ToolFailed))
        assert failed.name == "x_jot"
        assert "directly, outside `run_code`" in failed.error

    async def test_a_level_that_now_withholds_the_tool_says_so_rather_than_the_way_round(self):
        """At Plan a direct call is refused too, so "call it directly" would mislead the model."""
        calls, run = await self._narrowed_to("plan")

        assert calls == []
        failed = next(f for f in _frames(run) if isinstance(f, ToolFailed))
        assert failed.name == "x_jot"
        assert failed.error == blocked_message("plan", "x_jot")

    async def test_a_call_deferred_for_someone_else_to_run_fails_with_the_way_round(self):
        """A deferred call can't be awaited in a script: it fails in place and its row settles."""
        calls: list[str] = []
        categories = _gated_categories(calls)
        elsewhere: FunctionToolset[RunDeps] = FunctionToolset()

        @elsewhere.tool_plain(metadata={"sensitivity": "read"})
        def handoff() -> str:
            raise CallDeferred()

        categories["e"] = elsewhere
        code = (
            'out = ""\ntry:\n    await e_handoff()\nexcept Exception as e:\n    out = str(e)\nout'
        )
        model, _ = _script_then_answer(code)
        run, result, _ = await _drive(model, categories)

        assert not isinstance(result.output, DeferredToolRequests)
        nested = [f for f in _frames(run) if f.parent_tool_call_id == "s"]
        assert [f.type for f in nested] == ["tool.started", "tool.failed"]
        nested_failure = nested[1]
        assert isinstance(nested_failure, ToolFailed)
        assert "directly, outside `run_code`" in nested_failure.error
        assert "directly, outside `run_code`" in s(_result_of(_script_result(run)))

    async def test_the_call_cap_is_enforced(self):
        calls: list[str] = []
        code = 'for n in ["a", "b", "c"]:\n    await x_look(name=n)\n"ok"'
        model, seen = _script_then_answer(code)
        _ = await _drive(model, _gated_categories(calls), limits=CodeModeLimits(max_tool_calls=2))

        assert calls == ["look:a", "look:b"]
        retry = next(
            part
            for message in seen[1]
            if isinstance(message, ModelRequest)
            for part in message.parts
            if isinstance(part, ToolReturnPart) and part.outcome == "retried"
        )
        assert "2 nested tool calls" in retry.model_response_str()

    async def test_a_reload_rebuilds_the_scripts_rows(self):
        """The cold view rebuilds the script's calls from its own result, after store
        serialization turns parts into dicts."""
        from pydantic import TypeAdapter
        from pydantic_ai.messages import ModelMessage

        calls: list[str] = []
        code = (
            'a, b = await asyncio.gather(x_look(name="a"), x_look(name="b"))\n'
            "try:\n"
            '    await x_recall(query="q")\n'
            "except Exception:\n"
            "    pass\n"
            "[a, b]"
        )
        model, _ = _script_then_answer("import asyncio\n" + code)
        _, result, _ = await _drive(model, _gated_categories(calls))

        adapter = TypeAdapter(list[ModelMessage])
        stored = adapter.validate_json(adapter.dump_json(result.all_messages()))
        returned = next(
            part
            for message in stored
            if isinstance(message, ModelRequest)
            for part in message.parts
            if isinstance(part, ToolReturnPart) and part.tool_name == RUN_CODE
        )
        assert _METADATA.validate_python(vars(returned)["metadata"])["code_mode"] is True

        for messages in (result.all_messages(), stored):
            views = project_tree([(str(i), m) for i, m in enumerate(messages)])
            tools = views[-1].tools
            assert [t.name for t in tools] == [RUN_CODE, "x_look", "x_look", "x_recall"]
            script, *nested = tools
            assert script.parent_tool_call_id is None
            assert {t.parent_tool_call_id for t in nested} == {"s"}
            assert [t.status for t in nested] == ["ok", "ok", "error"]
            assert nested[0].args == {"name": "a"} and _result_of(nested[0]) == "saw a"
            assert nested[2].error is not None
            assert "directly, outside `run_code`" in nested[2].error

        from routes.conversations import message_out

        out = message_out(project_tree([(str(i), m) for i, m in enumerate(stored)])[-1], {})
        assert [t.parent_tool_call_id for t in out.tools] == [None, "s", "s", "s"]
        assert out.model_dump()["tools"][1]["parent_tool_call_id"] == "s"
