"""A tool result past the spill threshold has its oversized string leaves saved to scratch.

Driven through the real agent so the wrapper order is the app's: the recovering layer
spills every tool but a script's nested calls, and `run_code`'s own result is spilled where
the code-mode toolset returns it.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest
from pydantic_ai import FunctionToolset, ToolReturn
from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel
from pydantic_ai.toolsets.abstract import AbstractToolset

from agent import stream_agent_run
from agent.factory import build_agent
from core.citations import Citable, Citation
from core.config import Settings
from core.container import ServiceContainer
from core.untrusted import wrap_untrusted
from runs import Run, RunStream
from services.sandbox import SandboxSessionManager
from tools import RunDeps
from tools.code_mode_marks import RUN_CODE
from tools.files import files_toolset

LIMIT = 1000
BIG = "".join(f"line {i:04d}\n" for i in range(400))
SOURCE = "https://example.test/page"
PERSONAL = ("mail", "calendar", "memory", "vault", "corpus", "platform")


@dataclass
class _Found:
    text: str

    def citations(self) -> Sequence[Citation]:
        return [Citation(url=SOURCE, title="page")]


class _Session:
    def __init__(self, workspace: Path, *, fail: bool) -> None:
        self._workspace: Path = workspace
        self._fail: bool = fail

    def ensure_workspace(self) -> Path:
        self._workspace.mkdir(parents=True, exist_ok=True)
        return self._workspace

    def read_file(self, relpath: str) -> bytes:
        return (self._workspace / relpath).read_bytes()

    def write_file(self, relpath: str, content: bytes) -> None:
        if self._fail:
            raise OSError("disk full")
        target = self._workspace / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        _ = target.write_bytes(content)


class _Manager:
    def __init__(self, root: Path, *, fail: bool = False) -> None:
        self._root: Path = root
        self._fail: bool = fail
        self.sessions: dict[str, _Session] = {}

    async def acquire(self, key: str, *, holder: object = None) -> _Session:
        del holder
        return self.sessions.setdefault(key, _Session(self._root / key, fail=self._fail))


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
        return wrap_untrusted(BIG, source=SOURCE)

    @x.tool_plain(metadata={"sensitivity": "read"})
    def record() -> dict[str, object]:
        return {"exit_code": 0, "ok": True, "stdout": BIG, "stderr": "warn"}

    @x.tool_plain(metadata={"sensitivity": "read"})
    def page() -> dict[str, object]:
        return {"n": 1, "body": wrap_untrusted(BIG, source=SOURCE)}

    @x.tool_plain(metadata={"sensitivity": "read"})
    def mixed() -> str:
        return f"TRUSTED HEADER\n{wrap_untrusted(BIG, source=SOURCE)}\nTRUSTED FOOTER"

    @x.tool_plain(metadata={"sensitivity": "read"})
    def cited() -> _Found:
        return _Found(text=BIG)

    @x.tool_plain(metadata={"sensitivity": "read"})
    def returned() -> ToolReturn[str]:
        return ToolReturn(return_value=BIG, content="a side note", metadata={"k": 1})

    categories: dict[str, AbstractToolset[RunDeps]] = {"x": x, "files": files_toolset()}
    for category in PERSONAL:
        personal: FunctionToolset[RunDeps] = FunctionToolset()

        @personal.tool_plain(metadata={"sensitivity": "read"})
        def read() -> str:
            return BIG

        categories[category] = personal
    return categories


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
    workspace_key: str = "",
    fail: bool = False,
) -> tuple[list[ModelMessage], Path]:
    caps = caps or ServiceContainer()
    caps.add(_Manager(tmp_path, fail=fail), as_type=SandboxSessionManager)
    agent = build_agent(_model(first, args), categories=_categories())
    run = Run(id="t", kind="chat", owner_id="operator", stream=RunStream())
    deps = RunDeps(
        run=run,
        owner_id="operator",
        permission="yolo",
        caps=caps,
        conversation_id="c1",
        project_id=project_id,
        workspace_key=workspace_key,
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


def _content(messages: list[ModelMessage], tool: str) -> object:
    [part] = _returns(messages, tool)
    return cast(object, part.content)


def _saved(workspace: Path) -> list[Path]:
    return sorted((workspace / "tool-output").glob("*.txt"))


async def test_a_result_under_the_threshold_passes_unchanged(tmp_path: Path):
    messages, workspace = await _run_tool(tmp_path, "x_small")
    assert _content(messages, "x_small") == "tiny"
    assert _saved(workspace) == []


async def test_a_string_result_is_one_leaf_saved_whole_and_previewed(tmp_path: Path):
    messages, workspace = await _run_tool(tmp_path, "x_big")
    text = str(_content(messages, "x_big"))
    [saved] = _saved(workspace)
    assert re.fullmatch(r"call_1-[0-9a-f]{8}\.txt", saved.name)
    assert saved.read_text() == BIG
    assert f"/home/agent/scratch/tool-output/{saved.name}" in text
    assert "line 0000" in text and "line 0399" in text and "line 0200" not in text
    assert len(text) < len(BIG)


async def test_a_structured_result_keeps_its_shape_and_spills_the_big_leaf(tmp_path: Path):
    messages, workspace = await _run_tool(tmp_path, "x_record")
    content = cast(Mapping[str, object], _content(messages, "x_record"))
    assert content["exit_code"] == 0 and content["ok"] is True and content["stderr"] == "warn"
    stdout = str(content["stdout"])
    assert "tool-output/" in stdout and len(stdout) < len(BIG)
    [saved] = _saved(workspace)
    assert saved.read_text() == BIG
    assert len(saved.read_text().splitlines()) == 400


async def test_an_untrusted_leaf_is_rewrapped_with_its_source_and_never_half_fenced(
    tmp_path: Path,
):
    messages, workspace = await _run_tool(tmp_path, "x_page")
    content = cast(Mapping[str, object], _content(messages, "x_page"))
    assert content["n"] == 1
    body = str(content["body"])
    lines = body.splitlines()
    assert lines[0].startswith("Content inside <untrusted")
    assert lines[1].startswith('<untrusted id="') and f'source="{SOURCE}"' in lines[1]
    close = next(i for i, line in enumerate(lines) if line.startswith('</untrusted id="'))
    assert "tool-output/" in "\n".join(lines[close + 1 :])
    assert body.count('<untrusted id="') == 2 and body.count('</untrusted id="') == 1
    [saved] = _saved(workspace)
    assert saved.read_text().count("\n<untrusted id=") == 1


async def test_a_plain_leaf_is_never_wrapped(tmp_path: Path):
    messages, _ = await _run_tool(tmp_path, "x_record")
    content = cast(Mapping[str, object], _content(messages, "x_record"))
    assert "untrusted" not in str(content["stdout"])


async def test_trusted_text_beside_a_fenced_excerpt_stays_trusted(tmp_path: Path):
    messages, _ = await _run_tool(tmp_path, "x_mixed")
    text = str(_content(messages, "x_mixed"))
    assert text.startswith("TRUSTED HEADER\n")
    fence_open = text.index('<untrusted id="')
    fence_close = text.index('</untrusted id="')
    assert "TRUSTED FOOTER" in text[fence_close:]
    assert "TRUSTED HEADER" not in text[fence_open:fence_close]
    assert "line 0000" in text[fence_open:fence_close]


async def test_a_citable_result_keeps_its_type_and_citations(tmp_path: Path):
    messages, workspace = await _run_tool(tmp_path, "x_cited")
    found = _content(messages, "x_cited")
    assert isinstance(found, _Found) and isinstance(found, Citable)
    assert found.citations()[0].url == SOURCE
    assert "tool-output/" in found.text
    assert _saved(workspace)[0].read_text() == BIG


async def test_a_tool_return_keeps_its_content_and_metadata(tmp_path: Path):
    messages, workspace = await _run_tool(tmp_path, "x_returned")
    [part] = _returns(messages, "x_returned")
    assert "tool-output/" in str(part.content)
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


@pytest.mark.parametrize("category", PERSONAL)
async def test_personal_categories_are_not_spilled(tmp_path: Path, category: str):
    messages, workspace = await _run_tool(tmp_path, f"{category}_read")
    assert _content(messages, f"{category}_read") == BIG
    assert _saved(workspace) == []


async def test_a_file_read_is_not_spilled(tmp_path: Path):
    workspace = tmp_path / "c1"
    workspace.mkdir()
    _ = (workspace / "big.txt").write_text(BIG)
    messages, _ = await _run_tool(
        tmp_path, "files_read_file", {"path": "~/scratch/big.txt", "limit": 400}
    )
    assert "line 0399" in str(_content(messages, "files_read_file"))
    assert not (workspace / "tool-output").exists()


async def test_a_nested_call_gets_the_full_value_and_the_scripts_result_is_spilled(
    tmp_path: Path,
):
    messages, workspace = await _run_tool(tmp_path, RUN_CODE, {"code": "r = await x_big()\nlen(r)"})
    assert str(len(BIG)) in str(_content(messages, RUN_CODE))
    assert _saved(workspace) == []

    messages, workspace = await _run_tool(tmp_path / "again", RUN_CODE, {"code": "await x_big()"})
    [part] = _returns(messages, RUN_CODE)
    [saved] = _saved(workspace)
    assert BIG in saved.read_text()
    assert "tool-output/" in str(part.content)
    assert cast(Mapping[str, object], vars(part)["metadata"])["code_mode"] is True


async def test_a_script_that_called_personal_data_is_not_spilled(tmp_path: Path):
    messages, workspace = await _run_tool(tmp_path, RUN_CODE, {"code": "await mail_read()"})
    assert BIG in str(_content(messages, RUN_CODE))
    assert _saved(workspace) == []


async def test_a_failed_save_gives_a_preview_without_a_path(tmp_path: Path):
    messages, workspace = await _run_tool(tmp_path, "x_big", fail=True)
    text = str(_content(messages, "x_big"))
    assert "could not be saved" in text and "/home/agent" not in text
    assert "line 0000" in text and "line 0399" in text and len(text) < len(BIG)
    assert _saved(workspace) == []


async def test_a_sub_agent_thread_spills_into_its_owners_scratch(tmp_path: Path):
    messages, workspace = await _run_tool(tmp_path, "x_big", workspace_key="c1/w7")
    assert "tool-output/" in str(_content(messages, "x_big"))
    assert len(_saved(workspace)) == 1
    assert not (tmp_path / "c1/w7").exists()


async def _read_back(
    tmp_path: Path,
    tool: str = "x_big",
    caps: ServiceContainer | None = None,
    project_id: str | None = None,
) -> str:
    messages, _ = await _run_tool(tmp_path, tool, caps=caps, project_id=project_id)
    found = re.search(r"/home/agent/scratch/\S+?\.txt", str(_content(messages, tool)))
    assert found is not None
    path = found.group()

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
    return str(_content(agent_run.result.all_messages(), "files_read_file"))


async def test_files_read_file_reads_the_spilled_path_on_a_plain_thread(tmp_path: Path):
    assert "line 0399" in await _read_back(tmp_path)


async def test_files_read_file_reads_a_structured_leafs_file(tmp_path: Path):
    assert "line 0399" in await _read_back(tmp_path, "x_record")


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
    assert "line 0399" in await _read_back(tmp_path / "ws", caps=caps, project_id=created.id)
