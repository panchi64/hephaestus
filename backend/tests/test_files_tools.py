"""The sandbox filesystem tools: bound to the run's own workspace, contained to it,
and degrading rather than failing when no sandbox runtime exists.

The tools themselves are `pydantic_ai_harness`'s, so their internals aren't retested
here. What *is* ours is the binding — which directory a given run's call acts on — and
that is exactly what a shared, app-lifetime toolset object could get wrong.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from core.container import ServiceContainer
from runs import Run, RunStream
from services.sandbox import SandboxSessionManager
from tools import RunDeps, build_agent_toolsets, harness_events_capability
from tools.files import files_toolset


class _Session:
    """A session whose workspace is a real directory on disk."""

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace
        self.ensured = 0

    def ensure_workspace(self) -> Path:
        self.ensured += 1
        self._workspace.mkdir(parents=True, exist_ok=True)
        return self._workspace


class _Manager:
    """Hands out one session per key, each with its own workspace directory."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self.sessions: dict[str, _Session] = {}

    async def acquire(self, key: str, *, holder: object = None) -> _Session:
        if key not in self.sessions:
            self.sessions[key] = _Session(self._root / key)
        return self.sessions[key]


def _deps(manager: _Manager | None, conversation_id: str) -> tuple[Run, RunDeps]:
    run = Run(id="t", kind="chat", owner_id="operator", stream=RunStream())
    caps = ServiceContainer()
    if manager is not None:
        caps.add(manager, as_type=SandboxSessionManager)
    return run, RunDeps(run=run, owner_id="operator", caps=caps, conversation_id=conversation_id)


# ONE toolset for the whole module, exactly as the app builds one for its whole lifetime.
# That is the invariant worth testing: the category object is shared by every conversation
# while the workspace it acts on is not, and a per-call toolset would quietly pass even if
# the binding were global.
_TOOLSET = build_agent_toolsets({"files": files_toolset()})[0]


async def _call(tool: str, args: dict, *, manager, conversation_id="conv-1"):
    """Invoke one file tool with exact arguments through the real gated toolset stack.

    Driven directly rather than through a model: these assertions are about what a
    *specific* argument set does, and a `TestModel` synthesizes its own arguments.
    """
    toolset = _TOOLSET
    _run, deps = _deps(manager, conversation_id)
    # Two fields make this stand in for a tool call *inside* a run, both because the harness
    # filesystem tools emit capability events: `emit` refuses a context with no event stream
    # to emit into (a plain list is the shape the library carries), and it refuses an event it
    # cannot attribute to a capability. A real run attributes through `tool_manager`, which a
    # synthetic context has none of, so name the owner directly.
    ctx = RunContext(
        deps=deps,
        model=TestModel(),
        usage=RunUsage(),
        _event_stream_buffer=[],
        _capability=harness_events_capability(),
    )
    tools = await toolset.get_tools(ctx)
    name = f"files_{tool}"
    return await toolset.call_tool(name, args, ctx, tools[name])


async def test_each_conversation_reads_and_writes_its_own_workspace(tmp_path):
    manager = _Manager(tmp_path)
    (tmp_path / "conv-1").mkdir()
    (tmp_path / "conv-2").mkdir()
    (tmp_path / "conv-1" / "note.txt").write_text("first thread")
    (tmp_path / "conv-2" / "note.txt").write_text("second thread")

    one = await _call("read_file", {"path": "note.txt"}, manager=manager, conversation_id="conv-1")
    two = await _call("read_file", {"path": "note.txt"}, manager=manager, conversation_id="conv-2")

    # The category object is shared app-wide; the workspace it acts on is not.
    assert "first thread" in str(one)
    assert "second thread" in str(two)


async def test_write_lands_in_the_conversation_workspace(tmp_path):
    manager = _Manager(tmp_path)
    await _call("write_file", {"path": "result.txt", "content": "hello"}, manager=manager)
    assert (tmp_path / "conv-1" / "result.txt").read_text() == "hello"


async def test_paths_outside_the_workspace_are_refused(tmp_path):
    manager = _Manager(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("not the agent's")

    # Refusal is a `ModelRetry`, not a returned string: the model is told to correct the
    # path rather than handed a value it might mistake for the file's contents.
    for path in ("../secret.txt", str(secret)):
        with pytest.raises(ModelRetry) as caught:
            await _call("read_file", {"path": path}, manager=manager)
        assert "not the agent's" not in str(caught.value)


async def test_a_symlink_out_of_the_workspace_is_refused(tmp_path):
    manager = _Manager(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("host side")
    (tmp_path / "conv-1").mkdir(exist_ok=True)
    (tmp_path / "conv-1" / "link.txt").symlink_to(outside)

    # Containment resolves symlinks before authorizing, so a link is not a way out —
    # the case a naive prefix check on the *unresolved* path would miss.
    with pytest.raises(ModelRetry) as caught:
        await _call("read_file", {"path": "link.txt"}, manager=manager)
    assert "host side" not in str(caught.value)


async def test_a_correctable_failure_comes_back_as_a_retry_not_a_dead_run(tmp_path):
    """The harness hands back what the model can fix — a missing file as a plain result,
    a failed edit as `ModelRetry` — so the turn continues and the agent tries something
    else.

    Pinned here because it is behaviour we *inherit* from the harness, so nothing else in
    this repo would notice it going away. Also pinned: the message names the path the way
    the model spelled it, never an absolute host path.
    """
    manager = _Manager(tmp_path)
    (tmp_path / "conv-1").mkdir(exist_ok=True)

    missing = await _call("read_file", {"path": "nope.txt"}, manager=manager)
    assert "nope.txt" in missing
    assert str(tmp_path) not in missing

    with pytest.raises(ModelRetry):
        await _call(
            "edit_file",
            {"path": "nope.txt", "old_text": "a", "new_text": "b"},
            manager=manager,
        )


async def test_no_sandbox_runtime_degrades_instead_of_failing(tmp_path):
    # Fail-closed means the capability is absent, not that the turn dies: the model is
    # told its machine is unavailable and adapts.
    result = await _call("read_file", {"path": "note.txt"}, manager=None)
    assert "unavailable" in str(result).lower()


async def test_reading_materializes_the_workspace_without_a_container(tmp_path):
    manager = _Manager(tmp_path)
    await _call("list_directory", {"path": "."}, manager=manager)
    # The session was asked for its workspace (which restores a sealed one), and nothing
    # else — browsing files must not pay for a container start.
    assert manager.sessions["conv-1"].ensured >= 1
