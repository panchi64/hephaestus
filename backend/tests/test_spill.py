"""A tool result past the spill threshold is saved to scratch and replaced by a preview.

Driven through the real agent so the wrapper order is the app's: the recovering layer
spills every tool but a script's nested calls, and `run_code`'s own result is spilled where
the code-mode toolset returns it.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import cast

import pytest
from pydantic_ai import FunctionToolset, ToolReturn
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel
from pydantic_ai.toolsets.abstract import AbstractToolset

from agent import stream_agent_run
from agent.factory import build_agent
from core.config import Settings
from core.container import ServiceContainer
from core.untrusted import wrap_untrusted
from runs import Run, RunStream
from services.sandbox import SandboxSessionManager
from tools import RunDeps
from tools.files import files_toolset
from tools.spill import RUN_CODE

LIMIT = 200
BIG = "".join(f"line {i:04d}\n" for i in range(400))


class _Session:
    def __init__(self, workspace: Path) -> None:
        self._workspace: Path = workspace

    def ensure_workspace(self) -> Path:
        self._workspace.mkdir(parents=True, exist_ok=True)
        return self._workspace

    def read_file(self, relpath: str) -> bytes:
        return (self._workspace / relpath).read_bytes()

    def write_file(self, relpath: str, content: bytes) -> None:
        target = self._workspace / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        _ = target.write_bytes(content)


class _Manager:
    def __init__(self, root: Path) -> None:
        self._root: Path = root
        self.sessions: dict[str, _Session] = {}

    async def acquire(self, key: str, *, holder: object = None) -> _Session:
        del holder
        return self.sessions.setdefault(key, _Session(self._root / key))


@pytest.fixture(autouse=True)
def _threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(tool_output_spill_chars=LIMIT)
    monkeypatch.setattr("tools.spill.get_settings", lambda: settings)


def _categories() -> dict[str, AbstractToolset[RunDeps]]:
    x: FunctionToolset[RunDeps] = FunctionToolset()

    @x.tool_plain(metadata={"sensitivity": "read"})
    def small() -> str:
        return "tiny"

    @x.tool_plain(metadata={"sensitivity": "read"})
    def big() -> str:
        return BIG

    @x.tool_plain(metadata={"sensitivity": "read"})
    def fenced() -> str:
        return wrap_untrusted(BIG, source="https://example.test/page")

    @x.tool_plain(metadata={"sensitivity": "read"})
    def rows() -> list[dict[str, str]]:
        return [{"row": f"value {i}"} for i in range(100)]

    @x.tool_plain(metadata={"sensitivity": "read"})
    def returned() -> ToolReturn[str]:
        return ToolReturn(return_value=BIG, content="a side note", metadata={"k": 1})

    mail: FunctionToolset[RunDeps] = FunctionToolset()

    @mail.tool_plain(metadata={"sensitivity": "read"})
    def read() -> str:
        return BIG

    return {"x": x, "mail": mail, "files": files_toolset()}


def _model(first: str, args: dict[str, object] | None = None):
    seen: list[list[ModelMessage]] = []

    async def stream_fn(
        messages: list[ModelMessage], _info: AgentInfo
    ) -> AsyncIterator[str | dict[int, DeltaToolCall]]:
        seen.append(messages)
        if len(seen) == 1:
            yield {
                0: DeltaToolCall(
                    name=first, json_args=json.dumps(args or {}), tool_call_id="call/1"
                )
            }
        else:
            yield "done"

    return FunctionModel(stream_function=stream_fn)


async def _run_tool(
    tmp_path: Path,
    first: str,
    args: dict[str, object] | None = None,
    *,
    caps: ServiceContainer | None = None,
    project_id: str | None = None,
) -> tuple[list[ModelMessage], Path]:
    caps = caps or ServiceContainer()
    caps.add(_Manager(tmp_path), as_type=SandboxSessionManager)
    agent = build_agent(_model(first, args), categories=_categories())
    run = Run(id="t", kind="chat", owner_id="operator", stream=RunStream())
    deps = RunDeps(
        run=run,
        owner_id="operator",
        permission="yolo",
        caps=caps,
        conversation_id="c1",
        project_id=project_id,
    )
    async with agent.iter("go", deps=deps) as agent_run:
        await stream_agent_run(agent_run, run)
    assert agent_run.result is not None
    return agent_run.result.all_messages(), tmp_path / "c1"


def _returns(messages: list[ModelMessage], tool: str) -> list[ToolReturnPart]:
    return [
        part
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart) and part.tool_name == tool
    ]


def _saved(workspace: Path) -> list[Path]:
    return sorted((workspace / "tool-output").glob("*.txt"))


async def test_a_result_under_the_threshold_passes_unchanged(tmp_path: Path):
    messages, workspace = await _run_tool(tmp_path, "x_small")
    [part] = _returns(messages, "x_small")
    assert part.content == "tiny"
    assert _saved(workspace) == []


async def test_a_result_over_the_threshold_is_saved_whole_and_previewed(tmp_path: Path):
    messages, workspace = await _run_tool(tmp_path, "x_big")
    [part] = _returns(messages, "x_big")
    [saved] = _saved(workspace)
    assert saved.name == "call_1.txt"
    assert saved.read_text() == BIG
    text = str(part.content)
    assert "/home/agent/scratch/tool-output/call_1.txt" in text
    assert "line 0000" in text and "line 0399" in text
    assert "line 0200" not in text
    assert len(text) < len(BIG)


async def test_structured_values_are_saved_as_json(tmp_path: Path):
    messages, workspace = await _run_tool(tmp_path, "x_rows")
    [saved] = _saved(workspace)
    assert json.loads(saved.read_text())[99] == {"row": "value 99"}
    assert "tool-output/call_1.txt" in str(_returns(messages, "x_rows")[0].content)


async def test_an_untrusted_result_keeps_its_fence_in_the_file_and_is_rewrapped(tmp_path: Path):
    messages, workspace = await _run_tool(tmp_path, "x_fenced")
    [part] = _returns(messages, "x_fenced")
    [saved] = _saved(workspace)
    assert '\n<untrusted id="' in saved.read_text()
    text = str(part.content)
    lines = text.splitlines()
    assert lines[0].startswith("Content inside <untrusted")
    assert lines[1].startswith('<untrusted id="')
    close = next(i for i, line in enumerate(lines) if line.startswith('</untrusted id="'))
    assert "tool-output/call_1.txt" in "\n".join(lines[close + 1 :])
    assert "line 0000" in text


async def test_a_tool_return_keeps_its_content_and_metadata(tmp_path: Path):
    messages, workspace = await _run_tool(tmp_path, "x_returned")
    [part] = _returns(messages, "x_returned")
    assert "tool-output/call_1.txt" in str(part.content)
    assert vars(part)["metadata"] == {"k": 1}
    assert _saved(workspace)[0].read_text() == BIG
    side = [
        p.content
        for m in messages
        if isinstance(m, ModelRequest)
        for p in m.parts
        if isinstance(p, UserPromptPart)
    ]
    assert "a side note" in side


async def test_mail_results_are_not_spilled(tmp_path: Path):
    messages, workspace = await _run_tool(tmp_path, "mail_read")
    [part] = _returns(messages, "mail_read")
    assert part.content == BIG
    assert _saved(workspace) == []


async def test_a_file_read_is_not_spilled(tmp_path: Path):
    workspace = tmp_path / "c1"
    workspace.mkdir()
    _ = (workspace / "big.txt").write_text(BIG)
    messages, _ = await _run_tool(
        tmp_path, "files_read_file", {"path": "~/scratch/big.txt", "limit": 400}
    )
    [part] = _returns(messages, "files_read_file")
    assert "line 0399" in str(part.content)
    assert not (workspace / "tool-output").exists()


async def test_a_nested_call_gets_the_full_value_and_the_scripts_result_is_spilled(
    tmp_path: Path,
):
    code = "r = await x_big()\nlen(r)"
    messages, workspace = await _run_tool(tmp_path, RUN_CODE, {"code": code})
    [part] = _returns(messages, RUN_CODE)
    assert str(len(BIG)) in str(part.content)
    assert _saved(workspace) == []

    messages, workspace = await _run_tool(tmp_path / "again", RUN_CODE, {"code": "await x_big()"})
    [part] = _returns(messages, RUN_CODE)
    [saved] = _saved(workspace)
    assert BIG in saved.read_text()
    assert "tool-output/call_1.txt" in str(part.content)
    assert cast(Mapping[str, object], vars(part)["metadata"])["code_mode"] is True


async def _read_back(
    tmp_path: Path, caps: ServiceContainer | None, project_id: str | None = None
) -> str:
    messages, _ = await _run_tool(tmp_path, "x_big", caps=caps, project_id=project_id)
    note = str(_returns(messages, "x_big")[0].content)
    path = next(word for word in note.split() if word.startswith("/home/agent/scratch/"))
    path = path.rstrip(";.,")

    async def reader(
        msgs: list[ModelMessage], _info: AgentInfo
    ) -> AsyncIterator[str | dict[int, DeltaToolCall]]:
        if any(isinstance(m, ModelRequest) and _returns([m], "files_read_file") for m in msgs):
            yield "done"
        else:
            yield {
                0: DeltaToolCall(
                    name="files_read_file",
                    json_args=json.dumps({"path": path, "limit": 400}),
                    tool_call_id="r1",
                )
            }

    caps = caps or ServiceContainer()
    if caps.get_optional(SandboxSessionManager) is None:
        caps.add(_Manager(tmp_path), as_type=SandboxSessionManager)
    agent = build_agent(FunctionModel(stream_function=reader), categories=_categories())
    run = Run(id="t2", kind="chat", owner_id="operator", stream=RunStream())
    deps = RunDeps(
        run=run,
        owner_id="operator",
        permission="yolo",
        caps=caps,
        conversation_id="c1",
        project_id=project_id,
    )
    async with agent.iter("go", deps=deps) as agent_run:
        await stream_agent_run(agent_run, run)
    assert agent_run.result is not None
    [read] = _returns(agent_run.result.all_messages(), "files_read_file")
    return str(read.content)


async def test_files_read_file_reads_the_spilled_path_on_a_plain_thread(tmp_path: Path):
    assert "line 0399" in await _read_back(tmp_path, None)


async def test_files_read_file_reads_the_spilled_path_on_a_project_thread(tmp_path: Path):
    from core.db import init_db, make_engine
    from core.vault import Vault
    from services.home.changes import ThreadChanges
    from services.home.leases import WriteLeases
    from services.projects.store import ProjectStore
    from services.settings_store import SettingsStore

    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(tmp_path / "keyfile.json")
    await vault.setup("pw")
    projects = ProjectStore(engine, vault, SettingsStore(engine))
    root = tmp_path / "api"
    root.mkdir()
    created = await projects.create("operator", "api", str(root))
    caps = ServiceContainer()
    caps.add(projects)
    caps.add(WriteLeases(engine, ttl_s=3600))
    caps.add(ThreadChanges(engine, vault))
    assert "line 0399" in await _read_back(tmp_path / "ws", caps, created.id)
