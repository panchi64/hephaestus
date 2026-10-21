"""An edit of an existing file needs a read behind it, and fails if the file moved since."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from core.container import ServiceContainer
from core.db import init_db, make_engine
from core.vault import Vault
from runs import Run, RunStream
from services.home.leases import WriteLeases
from services.projects.store import ProjectStore
from services.sandbox import SandboxSessionManager
from services.settings_store import SettingsStore
from tools import RunDeps, build_agent_toolsets, harness_events_capability
from tools.files import files_toolset
from tools.read_ledger import ReadLedger, hash_reported

_TOOLSET = build_agent_toolsets({"files": files_toolset()})[0]
README = "~/projects/api/README.md"


class _Session:
    def __init__(self, workspace: Path) -> None:
        self._workspace: Path = workspace

    def ensure_workspace(self) -> Path:
        self._workspace.mkdir(parents=True, exist_ok=True)
        return self._workspace


class _Manager:
    """Hands out one session per key, each with its own scratch directory."""

    def __init__(self, root: Path) -> None:
        self._root: Path = root

    async def acquire(self, key: str, *, holder: object = None) -> _Session:
        del holder
        return _Session(self._root / "scratch" / key)


async def _project(tmp_path: Path) -> tuple[ServiceContainer, Path]:
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(tmp_path / "keyfile.json")
    await vault.setup("pw")
    projects = ProjectStore(engine, vault, SettingsStore(engine))
    root = tmp_path / "api"
    root.mkdir()
    _ = (root / "README.md").write_text("the api\n")
    _ = await projects.create("operator", "api", str(root))
    caps = ServiceContainer()
    caps.add(projects)
    caps.add(WriteLeases(engine, ttl_s=3600))
    caps.add(_Manager(tmp_path), as_type=SandboxSessionManager)
    return caps, root


async def _call(
    caps: ServiceContainer, tool: str, args: dict[str, object], *, conversation: str = "conv-1"
) -> str:
    run = Run(id="t", kind="chat", owner_id="operator", stream=RunStream())
    deps = RunDeps(run=run, owner_id="operator", caps=caps, conversation_id=conversation)
    ctx = RunContext(
        deps=deps,
        model=TestModel(),
        usage=RunUsage(),
        _event_stream_buffer=[],
        _capability=harness_events_capability(),
    )
    ctx.tool_call_approved = True
    tools = await _TOOLSET.get_tools(ctx)
    name = f"files_{tool}"
    return str(cast(object, await _TOOLSET.call_tool(name, args, ctx, tools[name])))


def _edit(path: str = README) -> dict[str, object]:
    return {"path": path, "old_text": "the api", "new_text": "the API"}


async def test_an_edit_with_no_read_is_refused(tmp_path: Path):
    caps, root = await _project(tmp_path)
    refused = await _call(caps, "edit_file", _edit())
    assert refused.startswith("Read ") and refused.endswith(" before changing it.")
    assert (root / "README.md").read_text() == "the api\n"

    overwrite = await _call(caps, "write_file", {"path": README, "content": "x"})
    assert "before changing it" in overwrite
    assert (root / "README.md").read_text() == "the api\n"


async def test_an_edit_after_a_read_goes_through(tmp_path: Path):
    caps, root = await _project(tmp_path)
    _ = await _call(caps, "read_file", {"path": README})
    done = await _call(caps, "edit_file", _edit())
    assert done.startswith("Edited")
    assert (root / "README.md").read_text() == "the API\n"


async def test_a_second_edit_needs_no_second_read(tmp_path: Path):
    caps, root = await _project(tmp_path)
    _ = await _call(caps, "read_file", {"path": README})
    _ = await _call(caps, "edit_file", _edit())
    again = await _call(
        caps, "edit_file", {"path": README, "old_text": "the API", "new_text": "an API"}
    )
    assert again.startswith("Edited")
    assert (root / "README.md").read_text() == "an API\n"


async def test_a_change_made_outside_after_the_read_is_a_conflict(tmp_path: Path):
    caps, root = await _project(tmp_path)
    _ = await _call(caps, "read_file", {"path": README})
    _ = (root / "README.md").write_text("changed by hand\n")

    outside: dict[str, object] = {**_edit(), "old_text": "changed", "new_text": "x"}
    with pytest.raises(ModelRetry, match="Conflict"):
        _ = await _call(caps, "edit_file", outside)
    assert (root / "README.md").read_text() == "changed by hand\n"

    _ = await _call(caps, "read_file", {"path": README})
    fixed = await _call(caps, "edit_file", outside)
    assert fixed.startswith("Edited")


async def test_the_models_own_hash_cannot_override_the_record(tmp_path: Path):
    caps, root = await _project(tmp_path)
    _ = await _call(caps, "read_file", {"path": README})
    _ = (root / "README.md").write_text("changed by hand\n")
    # A hash the model made up, or copied from an earlier read, would pass the harness's
    # check against the file as it is now; the ledger's own record is what is enforced.
    current = hash_reported("write_file", "Wrote 1 chars (1 lines) to x. [hash:aaaaaaaaaaaa]")
    forged: dict[str, object] = {
        **_edit(),
        "old_text": "changed",
        "new_text": "x",
        "expected_hash": current,
    }
    with pytest.raises(ModelRetry, match="Conflict"):
        _ = await _call(caps, "edit_file", forged)
    assert (root / "README.md").read_text() == "changed by hand\n"


async def test_a_new_file_needs_no_read_and_can_be_edited_after(tmp_path: Path):
    caps, root = await _project(tmp_path)
    made = await _call(caps, "write_file", {"path": "~/projects/api/new.md", "content": "one"})
    assert made.startswith("Wrote")
    edited = await _call(
        caps, "edit_file", {"path": "~/projects/api/new.md", "old_text": "one", "new_text": "two"}
    )
    assert edited.startswith("Edited")
    assert (root / "new.md").read_text() == "two"


async def test_a_write_after_a_read_goes_through(tmp_path: Path):
    caps, root = await _project(tmp_path)
    _ = await _call(caps, "read_file", {"path": README})
    done = await _call(caps, "write_file", {"path": README, "content": "rewritten\n"})
    assert done.startswith("Wrote")
    assert (root / "README.md").read_text() == "rewritten\n"


async def test_a_read_in_one_conversation_does_not_count_in_another(tmp_path: Path):
    caps, _ = await _project(tmp_path)
    _ = await _call(caps, "read_file", {"path": README}, conversation="a")
    refused = await _call(caps, "edit_file", _edit(), conversation="b")
    assert "before changing it" in refused


async def test_a_failed_read_records_nothing(tmp_path: Path):
    caps, _ = await _project(tmp_path)
    _ = await _call(caps, "read_file", {"path": "~/projects/api/missing.md"})
    ledger = caps.get(ReadLedger)
    assert ledger.get("conv-1", str(tmp_path / "api" / "missing.md")) is None


@pytest.mark.parametrize(
    ("tool", "result", "expected"),
    [
        ("read_file", "[a.md | 3 lines | hash:0123456789ab]\nbody", "0123456789ab"),
        ("write_file", "Wrote 3 chars (1 lines) to a.md. [hash:0123456789ab]", "0123456789ab"),
        ("edit_file", "Edited a.md. [hash:0123456789ab]", "0123456789ab"),
        ("read_file", "Path not found: a.md", None),
        ("edit_file", "Conflict: file 'a.md' has changed (expected hash:aaa, got hash:bbb)", None),
        ("list_directory", "[a.md | 3 lines | hash:0123456789ab]\n", None),
    ],
)
def test_the_hash_is_read_only_off_a_success(tool: str, result: str, expected: str | None):
    assert hash_reported(tool, result) == expected
