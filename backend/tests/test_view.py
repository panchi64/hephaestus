"""The unified ``view`` tool — one capability for the conversation's View.

``view_show(file=…)`` mints a version whose preview is the file's bytes and emits
``view.snapshot``; ``view_show(serve=…, port=…, path=…)`` runs the live head, emits
``view.live`` with the entry path baked into the url (so a static server's root no
longer renders a directory listing), and *also* overlays a fresh version
(``view.snapshot``) so the head has comparable code behind it; ``view_close`` emits
``view.live.stopped``. Bad arg combinations retry; missing capabilities degrade
without an event.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path

from pydantic_ai import Agent, DeferredToolRequests
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel

from agent import stream_agent_run
from core.container import ServiceContainer
from core.db import init_db, make_engine
from core.vault import Vault
from runs import EventBody, Run, RunStream
from services.artifacts import ArtifactStore
from services.home.scratch import ScratchDirs
from services.vm import PreviewHandle, Previews, VMError
from services.vm.machines import MachineSpec
from services.workspace_history import (
    SnapshotView,
    WorkspaceHistoryStore,
    format_show_result,
    snapshot_id_from_result,
)
from tools import RunDeps, build_agent_toolsets
from tools.view import view_toolset


class FakeManager:
    """A real scratch folder holding canned files, and a duck-typed stand-in for
    ``Previews`` (which needs a machine) that records the live-head launches."""

    def __init__(self, files: dict[str, bytes] | None = None, *, fail: bool = False) -> None:
        self.scratch: ScratchDirs = ScratchDirs(Path(tempfile.mkdtemp()))
        folder = self.scratch.acquire("conv-1")
        _ = folder.ensure_workspace()
        for relpath, content in (files or {}).items():
            folder.write_file(relpath, content)
        self.fail: bool = fail
        self.started: list[tuple[str, tuple[str, ...], int]] = []
        self.stopped: list[str] = []

    async def start(
        self, key: str, spec: MachineSpec, *, working_dir: str, command: list[str], port: int
    ) -> PreviewHandle:
        del spec, working_dir
        self.started.append((key, tuple(command), port))
        if self.fail:
            raise VMError("address already in use")
        return PreviewHandle(
            token="tok",
            host_port=5000,
            server_port=port,
            command=tuple(command),
        )

    async def stop(self, key: str) -> None:
        self.stopped.append(key)


async def _stores(tmp_path: Path) -> tuple[ArtifactStore, WorkspaceHistoryStore]:
    """An artifact store (preview bytes) and a workspace-history store (versions)
    over one in-memory engine + vault, wired into the run like the real engine does."""
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(tmp_path / "keyfile.json")
    await vault.setup("pw")
    return ArtifactStore(engine, vault), WorkspaceHistoryStore(engine, vault)


type _StreamFn = Callable[[list[ModelMessage], AgentInfo], AsyncIterator[str | DeltaToolCalls]]


def _call_then_finish(args: dict[str, object]) -> _StreamFn:
    """A model that calls ``view_show`` once with ``args``, then answers with text.
    Ends the loop on either a tool return or a validation retry, so a rejected call
    doesn't spin forever."""

    def _settled(messages: list[ModelMessage]) -> bool:
        return any(
            type(part).__name__ == "ToolReturnPart"
            for message in messages
            for part in message.parts
        )

    async def stream_fn(
        messages: list[ModelMessage], _info: AgentInfo
    ) -> AsyncIterator[str | DeltaToolCalls]:
        if _settled(messages):
            yield "done"
        else:
            yield {0: DeltaToolCall(name="view_show", json_args=json.dumps(args))}

    return stream_fn


def _close_then_finish() -> _StreamFn:
    def _settled(messages: list[ModelMessage]) -> bool:
        return any(
            type(part).__name__ == "ToolReturnPart"
            for message in messages
            for part in message.parts
        )

    async def stream_fn(
        messages: list[ModelMessage], _info: AgentInfo
    ) -> AsyncIterator[str | DeltaToolCalls]:
        if _settled(messages):
            yield "done"
        else:
            yield {0: DeltaToolCall(name="view_close", json_args="{}")}

    return stream_fn


def _run(
    stream_fn: _StreamFn,
    *,
    manager: FakeManager | None,
    store: ArtifactStore | None = None,
    history: WorkspaceHistoryStore | BrokenHistory | None = None,
    conversation_id: str = "conv-1",
) -> tuple[Agent[RunDeps, str | DeferredToolRequests], Run, RunDeps]:
    agent = Agent(
        FunctionModel(stream_function=stream_fn),
        deps_type=RunDeps,
        toolsets=build_agent_toolsets({"view": view_toolset()}),
        output_type=[str, DeferredToolRequests],
    )
    run = Run(id="r1", kind="chat", owner_id="operator", stream=RunStream())
    caps = ServiceContainer()
    if manager is not None:
        caps.add(manager.scratch)
        # The duck-typed fake registers under the class the view tools resolve.
        caps.add(manager, as_type=Previews)
    if store is not None:
        caps.add(store)
    if history is not None:
        caps.add(history)
    deps = RunDeps(
        run=run,
        owner_id="operator",
        caps=caps,
        conversation_id=conversation_id,
    )
    return agent, run, deps


def _bodies(run: Run) -> list[EventBody]:
    return [e.body for e in run.stream.replay()]


async def _drive(
    agent: Agent[RunDeps, str | DeferredToolRequests], run: Run, deps: RunDeps, prompt: str = "go"
) -> None:
    async with agent.iter(prompt, deps=deps) as agent_run:
        await stream_agent_run(agent_run, run)


# --- static versions (file=) -------------------------------------------------
async def test_show_file_captures_and_emits_snapshot(tmp_path: Path):
    store, history = await _stores(tmp_path)
    manager = FakeManager({"chart.png": b"\x89PNG\r\n"})
    agent, run, deps = _run(
        _call_then_finish({"file": "chart.png", "title": "My Chart"}),
        manager=manager,
        store=store,
        history=history,
    )
    await _drive(agent, run, deps, "make a chart")

    snap = next(b for b in _bodies(run) if b.type == "view.snapshot")
    assert snap.title == "My Chart"
    # A file-show stamps how it previews: the captured-bytes artifact + its render kind.
    assert snap.preview_kind == "image"
    assert snap.preview_artifact_id is not None
    blob = await store.content("operator", snap.preview_artifact_id)
    assert blob.content == b"\x89PNG\r\n"
    listed = await history.list("operator", "conv-1")
    assert [s.id for s in listed] == [snap.snapshot_id]


async def test_show_missing_file_reports_and_stores_nothing(tmp_path: Path):
    store, history = await _stores(tmp_path)
    manager = FakeManager({})
    agent, run, deps = _run(
        _call_then_finish({"file": "absent.html"}),
        manager=manager,
        store=store,
        history=history,
    )
    await _drive(agent, run, deps)
    assert "view.snapshot" not in [b.type for b in _bodies(run)]
    assert await history.list("operator", "conv-1") == []


async def test_show_file_unavailable_without_store():
    manager = FakeManager({"x.txt": b"x"})
    agent, run, deps = _run(
        _call_then_finish({"file": "x.txt"}), manager=manager, store=None, history=None
    )
    await _drive(agent, run, deps)
    assert "view.snapshot" not in [b.type for b in _bodies(run)]


# --- live head (serve=) ------------------------------------------------------
async def test_show_live_emits_live_with_entry_path(tmp_path: Path):
    _store, history = await _stores(tmp_path)
    manager = FakeManager()
    agent, run, deps = _run(
        _call_then_finish(
            {
                "serve": ["python", "-m", "http.server", "8000"],
                "port": 8000,
                "path": "index.html",
                "title": "Site",
            }
        ),
        manager=manager,
        history=history,
    )
    await _drive(agent, run, deps, "serve it")

    live = next(b for b in _bodies(run) if b.type == "view.live")
    # The entry path is baked into the url — this is the directory-listing fix.
    assert live.url == "/previews/tok/index.html"
    assert live.port == 8000
    assert live.command == "python -m http.server 8000"
    assert live.title == "Site"
    assert live.conversation_id == "conv-1"
    assert manager.started == [("conv-1", ("python", "-m", "http.server", "8000"), 8000)]

    # The live head now also overlays a fresh version (auto preview — no artifact bytes).
    snap = next(b for b in _bodies(run) if b.type == "view.snapshot")
    assert snap.title == "Site"
    assert snap.preview_artifact_id is None and snap.preview_kind is None
    assert [s.id for s in await history.list("operator", "conv-1")] == [snap.snapshot_id]


async def test_show_live_without_path_points_at_root():
    manager = FakeManager()
    agent, run, deps = _run(
        _call_then_finish({"serve": ["npm", "run", "dev"], "port": 5173}),
        manager=manager,
    )
    await _drive(agent, run, deps, "serve it")
    live = next(b for b in _bodies(run) if b.type == "view.live")
    assert live.url == "/previews/tok/"


async def test_show_live_failure_feeds_back_without_event():
    manager = FakeManager(fail=True)
    agent, run, deps = _run(_call_then_finish({"serve": ["bad"], "port": 8000}), manager=manager)
    await _drive(agent, run, deps, "serve it")
    assert "view.live" not in [b.type for b in _bodies(run)]
    assert manager.started  # it tried; the error went back to the model as text


class BrokenHistory:
    """A version store whose capture always fails — to prove history is best-effort."""

    async def capture(self, *_args: object, **_kwargs: object) -> SnapshotView:
        raise RuntimeError("vault locked")


async def test_show_live_emits_head_even_when_version_capture_fails():
    # A capture failure must never orphan the already-running server: the live event
    # still fires (and the turn doesn't error), the snapshot just doesn't get recorded.
    manager = FakeManager()
    agent, run, deps = _run(
        _call_then_finish({"serve": ["python", "-m", "http.server", "8000"], "port": 8000}),
        manager=manager,
        history=BrokenHistory(),
    )
    await _drive(agent, run, deps, "serve it")
    types = [b.type for b in _bodies(run)]
    assert "view.live" in types
    assert "view.snapshot" not in types


async def test_show_file_degrades_when_version_capture_fails(tmp_path: Path):
    # The artifact bytes are published, but a capture failure degrades to a message
    # instead of breaking the turn — and emits no half-formed version event.
    store, _ = await _stores(tmp_path)
    manager = FakeManager({"chart.png": b"\x89PNG\r\n"})
    agent, run, deps = _run(
        _call_then_finish({"file": "chart.png"}),
        manager=manager,
        store=store,
        history=BrokenHistory(),
    )
    await _drive(agent, run, deps, "make a chart")
    assert "view.snapshot" not in [b.type for b in _bodies(run)]


async def test_show_live_unavailable_without_sandbox():
    agent, run, deps = _run(_call_then_finish({"serve": ["x"], "port": 8000}), manager=None)
    await _drive(agent, run, deps, "serve it")
    assert "view.live" not in [b.type for b in _bodies(run)]


# --- close + validation ------------------------------------------------------
async def test_close_emits_live_stopped():
    manager = FakeManager()
    agent, run, deps = _run(_close_then_finish(), manager=manager)
    await _drive(agent, run, deps, "stop it")
    assert any(b.type == "view.live.stopped" for b in _bodies(run))
    assert manager.stopped == ["conv-1"]


async def test_show_both_modes_retries_without_showing_anything(tmp_path: Path):
    # file AND serve is ambiguous → ModelRetry, no view event either way.
    store, history = await _stores(tmp_path)
    manager = FakeManager({"x.html": b"<p>x</p>"})
    agent, run, deps = _run(
        _call_then_finish({"file": "x.html", "serve": ["x"], "port": 8000}),
        manager=manager,
        store=store,
        history=history,
    )
    await _drive(agent, run, deps)
    types = [b.type for b in _bodies(run)]
    assert "view.snapshot" not in types and "view.live" not in types


# --- result round-trip (cold-read re-attach contract) ------------------------
def test_show_result_round_trips_the_version_id():
    snap = SnapshotView(
        id="deadbeef01",
        conversation_id="conv-1",
        title="My Chart",
        created_at=datetime.now(UTC),
        files_changed=1,
        summary="+1 ~0 -0",
        stats={"added": 1, "modified": 0, "removed": 0},
        preview_artifact_id="a1",
        preview_kind="image",
        keeper=False,
    )
    assert snapshot_id_from_result(format_show_result(snap, "image")) == "deadbeef01"


def test_version_id_from_unrelated_result_is_none():
    assert snapshot_id_from_result("Could not read 'x.html': no such file") is None
