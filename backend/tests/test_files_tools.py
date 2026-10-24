"""The sandbox filesystem tools: bound to the run's own workspace, contained to it,
and degrading rather than failing when no sandbox runtime exists.

The tools themselves are `pydantic_ai_harness`'s, so their internals aren't retested
here. What *is* ours is the binding — which directory a given run's call acts on — and
that is exactly what a shared, app-lifetime toolset object could get wrong.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.exceptions import ApprovalRequired
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage
from pydantic_ai.workspaces import Workspace

from core.container import ServiceContainer
from core.db import init_db, make_engine
from core.vault import Vault
from runs import Run, RunStream
from services.home.changes import ThreadChanges
from services.home.leases import WriteLeases
from services.home.paths import Place
from services.home.scratch import ScratchDirs
from services.projects.store import ProjectStore
from services.settings_store import SettingsStore
from tools import RunDeps, build_agent_toolsets, harness_events_capability
from tools.files import files_toolset
from tools.home_workspace import home_workspace


def _deps(
    manager: ScratchDirs | None, conversation_id: str, extra: ServiceContainer | None = None
) -> tuple[Run, RunDeps]:
    run = Run(id="t", kind="chat", owner_id="operator", stream=RunStream())
    caps = extra or ServiceContainer()
    if manager is not None and caps.get_optional(ScratchDirs) is None:
        caps.add(manager)
    return run, RunDeps(run=run, owner_id="operator", caps=caps, conversation_id=conversation_id)


# ONE toolset for the whole module, exactly as the app builds one for its whole lifetime.
# That is the invariant worth testing: the category object is shared by every conversation
# while the workspace it acts on is not, and a per-call toolset would quietly pass even if
# the binding were global.
_TOOLSET = build_agent_toolsets({"files": files_toolset()})[0]


async def _call(
    tool: str,
    args: dict[str, object],
    *,
    manager: ScratchDirs | None,
    conversation_id: str = "conv-1",
    caps: ServiceContainer | None = None,
    approved: bool = False,
    delegated: bool = False,
    leases_from: str | None = None,
) -> object:
    """Invoke one file tool with exact arguments through the real gated toolset stack.

    Driven directly rather than through a model: these assertions are about what a
    *specific* argument set does, and a `TestModel` synthesizes its own arguments.
    """
    toolset = _TOOLSET
    _run, deps = _deps(manager, conversation_id, caps)
    deps.delegated_approved = delegated
    deps.leases_from = leases_from
    # Two fields make this stand in for a tool call *inside* a run, both because the harness
    # filesystem tools emit capability events: `emit` refuses a context with no event stream
    # to emit into (a plain list is the shape the library carries), and it refuses an event it
    # cannot attribute to a capability. A real run attributes through `tool_manager`, which a
    # synthetic context has none of, so name the owner directly.
    ctx = RunContext(
        deps=deps,
        workspace=Workspace(home_workspace(deps)),
        model=TestModel(),
        usage=RunUsage(),
        _event_stream_buffer=[],
        _capability=harness_events_capability(),
    )
    ctx.tool_call_approved = approved
    tools = await toolset.get_tools(ctx)
    name = f"files_{tool}"
    return cast(object, await toolset.call_tool(name, args, ctx, tools[name]))


async def _with_project(tmp_path: Path) -> tuple[ServiceContainer, Path, WriteLeases]:
    """Caps holding one registered project, ``~/projects/api``, and the write leases."""
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(tmp_path / "keyfile.json")
    await vault.setup("pw")
    projects = ProjectStore(engine, vault, SettingsStore(engine))
    root = tmp_path / "api"
    root.mkdir()
    _ = (root / "README.md").write_text("the api\n")
    _ = await projects.create("operator", "api", str(root))
    leases = WriteLeases(engine, ttl_s=3600)
    caps = ServiceContainer()
    caps.add(projects)
    caps.add(leases)
    caps.add(ThreadChanges(engine, vault))
    return caps, root, leases


async def test_a_sandbox_thread_reads_a_project_by_its_home_path(tmp_path: Path):
    caps, root, _leases = await _with_project(tmp_path)
    _ = (root / "CLAUDE.md").write_text("Run the checks with `make check`.\n")
    read = await _call(
        "read_file", {"path": "~/projects/api/README.md"}, manager=ScratchDirs(tmp_path), caps=caps
    )
    # The first touch of the project carries how to work in it.
    assert "the api" in str(read) and "make check" in str(read)


async def test_a_listing_with_no_folder_named_is_of_the_threads_own_place(tmp_path: Path):
    caps, _root, _leases = await _with_project(tmp_path)
    manager = ScratchDirs(tmp_path)
    _ = await _call("write_file", {"path": "mine.txt", "content": "x"}, manager=manager, caps=caps)
    listed = str(await _call("list_directory", {}, manager=manager, caps=caps))
    assert "mine.txt" in listed and "README.md" not in listed
    home = str(await _call("list_directory", {"path": "~"}, manager=manager, caps=caps))
    # The thread's scratch is its working directory, so the listing names it as `./`.
    assert "../projects/" in home and "./" in home and "../shared/" in home


async def test_the_scratch_alias_names_the_same_file(tmp_path: Path):
    # `/work/...` is how the code tool and a sandbox workspace name scratch, so a path one
    # tool hands back has to be one the file tools take.
    caps, _root, _leases = await _with_project(tmp_path)
    manager = ScratchDirs(tmp_path)
    args: dict[str, object] = {"path": "/work/report.md", "content": "ok"}
    _ = await _call("write_file", args, manager=manager, caps=caps)
    read = await _call("read_file", {"path": "~/scratch/report.md"}, manager=manager, caps=caps)
    assert "ok" in str(read)


async def test_a_relative_path_is_inside_the_threads_own_place(tmp_path: Path):
    # A folder of the thread's own named like a group of the home is still its own folder.
    caps, _root, _leases = await _with_project(tmp_path)
    manager = ScratchDirs(tmp_path)
    # With no such folder of its own, it is the home's: models drop the `~/`.
    dropped = await _call(
        "read_file", {"path": "projects/api/README.md"}, manager=manager, caps=caps
    )
    assert "the api" in str(dropped)
    (manager.path("conv-1") / "projects" / "api").mkdir(parents=True)
    args: dict[str, object] = {"path": "projects/api/x.md", "content": "mine"}
    _ = await _call("write_file", args, manager=manager, caps=caps)
    read = await _call(
        "read_file", {"path": "~/scratch/projects/api/x.md"}, manager=manager, caps=caps
    )
    assert "mine" in str(read)
    with pytest.raises(ModelRetry, match="climbs out"):
        _ = await _call("read_file", {"path": "../projects/api/README.md"}, manager=manager)


async def test_a_write_into_a_project_the_thread_may_only_read_asks_first(tmp_path: Path):
    caps, root, _leases = await _with_project(tmp_path)
    args: dict[str, object] = {"path": "~/projects/api/notes.md", "content": "hi"}
    with pytest.raises(ApprovalRequired):
        _ = await _call("write_file", args, manager=ScratchDirs(tmp_path), caps=caps)
    assert not (root / "notes.md").exists()


async def test_an_approved_write_leases_the_project_to_the_thread(tmp_path: Path):
    caps, root, leases = await _with_project(tmp_path)
    manager = ScratchDirs(tmp_path)
    args: dict[str, object] = {"path": "~/projects/api/notes.md", "content": "hi"}
    _ = await _call("write_file", args, manager=manager, caps=caps, approved=True)
    assert (root / "notes.md").read_text() == "hi"
    assert await leases.held("operator", "conv-1") == {"projects/api"}
    # A write to the operator's own folder is one they can put back.
    changes = await caps.get(ThreadChanges).list("operator", "conv-1")
    assert [(view.path, view.status) for view in changes] == [("notes.md", "added")]
    # The next write in a later run of the thread does not ask again.
    again: dict[str, object] = {"path": "~/projects/api/more.md", "content": "more"}
    _ = await _call("write_file", again, manager=manager, caps=caps)
    assert (root / "more.md").read_text() == "more"


async def test_a_delegated_agent_cannot_take_write_access(tmp_path: Path):
    caps, root, leases = await _with_project(tmp_path)
    args: dict[str, object] = {"path": "~/projects/api/notes.md", "content": "hi"}
    refused = await _call(
        "write_file", args, manager=ScratchDirs(tmp_path), caps=caps, approved=True, delegated=True
    )
    assert "read-only to you" in str(refused)
    assert not (root / "notes.md").exists()
    assert await leases.held("operator", "conv-1") == set()


async def test_each_conversation_reads_and_writes_its_own_workspace(tmp_path: Path):
    manager = ScratchDirs(tmp_path)
    (manager.path("conv-1")).mkdir()
    (manager.path("conv-2")).mkdir()
    _ = (manager.path("conv-1") / "note.txt").write_text("first thread")
    _ = (manager.path("conv-2") / "note.txt").write_text("second thread")

    one = await _call("read_file", {"path": "note.txt"}, manager=manager, conversation_id="conv-1")
    two = await _call("read_file", {"path": "note.txt"}, manager=manager, conversation_id="conv-2")

    # The category object is shared app-wide; the workspace it acts on is not.
    assert "first thread" in str(one)
    assert "second thread" in str(two)


async def test_write_lands_in_the_conversation_workspace(tmp_path: Path):
    manager = ScratchDirs(tmp_path)
    _ = await _call("write_file", {"path": "result.txt", "content": "hello"}, manager=manager)
    assert (manager.path("conv-1") / "result.txt").read_text() == "hello"


async def test_paths_outside_the_workspace_are_refused(tmp_path: Path):
    manager = ScratchDirs(tmp_path)
    secret = tmp_path / "secret.txt"
    _ = secret.write_text("not the agent's")

    # Refusal is a `ModelRetry`, not a returned string: the model is told to correct the
    # path rather than handed a value it might mistake for the file's contents.
    for path in ("../secret.txt", str(secret)):
        with pytest.raises(ModelRetry) as caught:
            _ = await _call("read_file", {"path": path}, manager=manager)
        assert "not the agent's" not in str(caught.value)


async def test_a_symlink_out_of_the_workspace_is_refused(tmp_path: Path):
    manager = ScratchDirs(tmp_path)
    outside = tmp_path / "outside.txt"
    _ = outside.write_text("host side")
    (manager.path("conv-1")).mkdir(exist_ok=True)
    (manager.path("conv-1") / "link.txt").symlink_to(outside)

    # Containment resolves symlinks before authorizing, so a link is not a way out —
    # the case a naive prefix check on the *unresolved* path would miss.
    with pytest.raises(ModelRetry) as caught:
        _ = await _call("read_file", {"path": "link.txt"}, manager=manager)
    assert "host side" not in str(caught.value)


async def test_a_correctable_failure_comes_back_as_a_retry_not_a_dead_run(tmp_path: Path):
    """The harness hands back what the model can fix — a missing file as a plain result,
    a failed edit as `ModelRetry` — so the turn continues and the agent tries something
    else.

    Pinned here because it is behaviour we *inherit* from the harness, so nothing else in
    this repo would notice it going away. Also pinned: the message names the path the way
    the model spelled it, never an absolute host path.
    """
    manager = ScratchDirs(tmp_path)
    (manager.path("conv-1")).mkdir(exist_ok=True)

    missing = await _call("read_file", {"path": "nope.txt"}, manager=manager)
    assert isinstance(missing, str)
    assert "nope.txt" in missing
    assert str(tmp_path) not in missing

    with pytest.raises(ModelRetry):
        _ = await _call(
            "edit_file",
            {"path": "nope.txt", "old_text": "a", "new_text": "b"},
            manager=manager,
        )


async def test_no_sandbox_runtime_degrades_instead_of_failing():
    # Fail-closed means the capability is absent, not that the turn dies: the model is
    # told its machine is unavailable and adapts.
    result = await _call("read_file", {"path": "note.txt"}, manager=None)
    assert "unavailable" in str(result).lower()


async def test_reading_materializes_the_workspace_without_the_vm(tmp_path: Path):
    manager = ScratchDirs(tmp_path)
    _ = await _call("list_directory", {"path": "."}, manager=manager)
    assert manager.settled_workspace("conv-1") is not None


async def test_a_sub_agent_writes_where_its_parent_may_and_nowhere_new(tmp_path: Path):
    caps, root, leases = await _with_project(tmp_path)
    manager = ScratchDirs(tmp_path)
    write: dict[str, object] = {"path": "~/projects/api/notes.md", "content": "hi"}

    async def child(*, approved: bool = False) -> object:
        return await _call(
            "write_file",
            write,
            manager=manager,
            caps=caps,
            conversation_id="child",
            approved=approved,
            leases_from="parent",
        )

    # Sharing its parent's files, it works under the parent's leases...
    refused = await child(approved=True)
    assert "read-only to you" in str(refused) and not (root / "notes.md").exists()
    await leases.lease("operator", "parent", Place("projects", "api"))
    _ = await child()
    assert (root / "notes.md").read_text() == "hi"
    # ...and an approval inside it never gives it, or its parent, a lease of its own.
    assert await leases.held("operator", "child") == set()
