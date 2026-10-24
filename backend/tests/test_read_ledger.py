"""An edit of an existing file needs a read behind it, and fails if the file moved since."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic_ai import ApprovalRequired, ModelRetry
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    ToolReturnPart,
)

from core.container import ServiceContainer
from services.home.leases import WriteLeases
from services.projects.store import ProjectStore
from services.sandbox import SandboxSessionManager
from tools.files import unread_refusal
from tools.read_ledger import ReadLedger, Seen, hash_reported, seed_from_history

from .files_support import README, call_files, project_caps, run_deps


def _edit(path: str = README) -> dict[str, object]:
    return {"path": path, "old_text": "the api", "new_text": "the API"}


async def test_an_edit_with_no_read_is_refused(tmp_path: Path):
    caps, root = await project_caps(tmp_path)
    refused = await call_files(caps, "edit_file", _edit())
    assert refused.startswith("error: Read ")
    assert "before changing it." in refused
    assert (root / "README.md").read_text() == "the api\n"

    overwrite = await call_files(caps, "write_file", {"path": README, "content": "x"})
    assert "before changing it" in overwrite
    assert (root / "README.md").read_text() == "the api\n"


async def test_an_edit_after_a_read_goes_through(tmp_path: Path):
    caps, root = await project_caps(tmp_path)
    _ = await call_files(caps, "read_file", {"path": README})
    done = await call_files(caps, "edit_file", _edit())
    assert done.startswith("Edited")
    assert (root / "README.md").read_text() == "the API\n"


async def test_a_second_edit_needs_no_second_read(tmp_path: Path):
    caps, root = await project_caps(tmp_path)
    _ = await call_files(caps, "read_file", {"path": README})
    _ = await call_files(caps, "edit_file", _edit())
    again = await call_files(
        caps, "edit_file", {"path": README, "old_text": "the API", "new_text": "an API"}
    )
    assert again.startswith("Edited")
    assert (root / "README.md").read_text() == "an API\n"


async def test_a_change_made_outside_after_the_read_is_a_conflict(tmp_path: Path):
    caps, root = await project_caps(tmp_path)
    _ = await call_files(caps, "read_file", {"path": README})
    _ = (root / "README.md").write_text("changed by hand\n")

    outside: dict[str, object] = {**_edit(), "old_text": "changed", "new_text": "x"}
    with pytest.raises(ModelRetry, match="Conflict"):
        _ = await call_files(caps, "edit_file", outside)
    assert (root / "README.md").read_text() == "changed by hand\n"

    _ = await call_files(caps, "read_file", {"path": README})
    fixed = await call_files(caps, "edit_file", outside)
    assert fixed.startswith("Edited")


async def test_the_models_own_hash_cannot_override_the_record(tmp_path: Path):
    caps, root = await project_caps(tmp_path)
    _ = await call_files(caps, "read_file", {"path": README})
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
        _ = await call_files(caps, "edit_file", forged)
    assert (root / "README.md").read_text() == "changed by hand\n"


async def test_a_new_file_needs_no_read_and_can_be_edited_after(tmp_path: Path):
    caps, root = await project_caps(tmp_path)
    made = await call_files(caps, "write_file", {"path": "~/projects/api/new.md", "content": "one"})
    assert made.startswith("Wrote")
    edited = await call_files(
        caps, "edit_file", {"path": "~/projects/api/new.md", "old_text": "one", "new_text": "two"}
    )
    assert edited.startswith("Edited")
    assert (root / "new.md").read_text() == "two"


async def test_a_write_after_a_read_goes_through(tmp_path: Path):
    caps, root = await project_caps(tmp_path)
    _ = await call_files(caps, "read_file", {"path": README})
    done = await call_files(caps, "write_file", {"path": README, "content": "rewritten\n"})
    assert done.startswith("Wrote")
    assert (root / "README.md").read_text() == "rewritten\n"


async def test_a_read_in_one_conversation_does_not_count_in_another(tmp_path: Path):
    caps, _ = await project_caps(tmp_path)
    _ = await call_files(caps, "read_file", {"path": README}, conversation="a")
    refused = await call_files(caps, "edit_file", _edit(), conversation="b")
    assert "before changing it" in refused


async def test_a_failed_read_records_nothing(tmp_path: Path):
    caps, _ = await project_caps(tmp_path)
    _ = await call_files(caps, "read_file", {"path": "~/projects/api/missing.md"})
    ledger = caps.get(ReadLedger)
    assert ledger.get("conv-1", str(tmp_path / "api" / "missing.md")) is None


async def test_a_binary_file_can_be_rewritten_once_read(tmp_path: Path):
    caps, root = await project_caps(tmp_path)
    blob = root / "logo.bin"
    _ = blob.write_bytes(b"\x00\x01\x02binary")
    path = "~/projects/api/logo.bin"
    refused = await call_files(caps, "write_file", {"path": path, "content": "text"})
    assert "before changing it" in refused

    read = await call_files(caps, "read_file", {"path": path})
    assert read.startswith("[Binary file")
    done = await call_files(caps, "write_file", {"path": path, "content": "text"})
    assert done.startswith("Wrote")
    assert blob.read_text() == "text"


async def test_a_guarded_write_to_an_unread_file_is_refused_before_it_asks(tmp_path: Path):
    caps, root = await project_caps(tmp_path)
    (root / ".git").mkdir()
    config = root / ".git" / "config"
    _ = config.write_text("[core]\n")
    args: dict[str, object] = {"path": "~/projects/api/.git/config", "content": "x"}
    refused = await call_files(caps, "write_file", args, approved=False)
    assert "before changing it" in refused
    assert config.read_text() == "[core]\n"

    _ = await call_files(caps, "read_file", {"path": args["path"]})
    with pytest.raises(ApprovalRequired):
        _ = await call_files(caps, "write_file", args, approved=False)


async def test_a_run_with_no_ledger_is_not_gated(tmp_path: Path):
    caps, root = await project_caps(tmp_path)
    bare = ServiceContainer()
    for kind in (ProjectStore, WriteLeases):
        bare.add(caps.get(kind))
    bare.add(caps.get(SandboxSessionManager), as_type=SandboxSessionManager)
    done = await call_files(bare, "edit_file", _edit())
    assert done.startswith("Edited")
    assert (root / "README.md").read_text() == "the API\n"


def test_recent_lists_named_text_files_newest_first():
    ledger = ReadLedger()
    ledger.record("c", "/a", "aaaaaaaaaaaa", named="~/a")
    ledger.record("c", "/seeded", "bbbbbbbbbbbb")
    ledger.record("c", "/img", "binary", named="~/img")
    ledger.record("c", "/b", "cccccccccccc", named="~/b")
    assert ledger.recent("c", 5) == ["~/b", "~/a"]
    assert ledger.recent("c", 1) == ["~/b"]
    assert ledger.recent("other", 5) == []


def test_reset_keeps_only_what_it_is_given():
    ledger = ReadLedger()
    ledger.record("c", "/a", "aaaaaaaaaaaa", named="~/a")
    ledger.record("c", "/b", "bbbbbbbbbbbb", named="~/b")
    ledger.reset("c", {"/b": Seen("dddddddddddd", "~/b")})
    assert ledger.get("c", "/a") is None
    assert ledger.get("c", "/b") == "dddddddddddd"


def test_the_ledger_forgets_the_oldest_conversation_and_path():
    ledger = ReadLedger()
    for number in range(300):
        ledger.record(f"c{number}", "/a", "0123456789ab")
    assert ledger.get("c0", "/a") is None
    assert ledger.get("c299", "/a") == "0123456789ab"
    for number in range(2100):
        ledger.record("one", f"/p{number}", "0123456789ab")
    assert ledger.get("one", "/p0") is None
    assert ledger.get("one", "/p2099") == "0123456789ab"


async def test_the_unread_refusal_is_available_before_anyone_is_asked(tmp_path: Path):
    caps, _ = await project_caps(tmp_path)
    deps = run_deps(caps)
    args: dict[str, object] = {"path": README, "content": "x"}
    assert await unread_refusal(deps, "files_write_file", args) is not None
    assert await unread_refusal(deps, "files_read_file", args) is None
    assert (
        await unread_refusal(deps, "files_write_file", {**args, "path": "~/projects/api/n"}) is None
    )
    _ = await call_files(caps, "read_file", {"path": README})
    assert await unread_refusal(deps, "files_write_file", args) is None


@pytest.mark.parametrize(
    ("tool", "result", "expected"),
    [
        ("read_file", "[Binary file: 12 bytes. Use a binary-aware tool to inspect.]", "binary"),
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


def _read_history(path: str, result: str) -> list[ModelMessage]:
    return [
        ModelResponse(parts=[ToolCallPart("files_read_file", {"path": path}, tool_call_id="r1")]),
        ModelRequest(parts=[ToolReturnPart("files_read_file", result, tool_call_id="r1")]),
    ]


async def test_an_edit_resumed_after_a_restart_is_seeded_from_the_parked_history(tmp_path: Path):
    caps, root = await project_caps(tmp_path)
    read = await call_files(caps, "read_file", {"path": README}, conversation="before")
    history = _read_history(README, read)

    unseeded = await call_files(caps, "edit_file", _edit(), conversation="after")
    assert "before changing it." in unseeded

    done = await call_files(caps, "edit_file", _edit(), conversation="after", messages=history)
    assert done.startswith("Edited")
    assert (root / "README.md").read_text() == "the API\n"


async def test_seeding_pins_the_history_hash_so_a_moved_file_still_conflicts(tmp_path: Path):
    caps, root = await project_caps(tmp_path)
    read = await call_files(caps, "read_file", {"path": README}, conversation="before")
    history = _read_history(README, read)
    _ = (root / "README.md").write_text("changed by hand\n")

    with pytest.raises(ModelRetry, match="Conflict"):
        _ = await call_files(
            caps,
            "edit_file",
            {**_edit(), "old_text": "changed"},
            conversation="after",
            messages=history,
        )
    assert (root / "README.md").read_text() == "changed by hand\n"


async def test_seeding_skips_a_history_call_with_no_path(tmp_path: Path):
    caps, _root = await project_caps(tmp_path)
    pathless: list[ModelMessage] = [
        ModelResponse(parts=[ToolCallPart("files_read_file", {}, tool_call_id="r1")]),
        ModelRequest(
            parts=[
                ToolReturnPart(
                    "files_read_file", "[a | 1 lines | hash:aaaaaaaaaaaa]\nx", tool_call_id="r1"
                )
            ]
        ),
    ]
    _ = await call_files(caps, "edit_file", _edit(), conversation="after", messages=pathless)
    assert caps.get(ReadLedger).get("after", "None") is None


async def test_a_write_to_a_file_the_ledger_knows_does_not_rescan_the_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    caps, _root = await project_caps(tmp_path)
    read = await call_files(caps, "read_file", {"path": README}, conversation="c")
    history = _read_history(README, read)
    scans: list[int] = []

    def counting(*_args: object) -> None:
        scans.append(1)

    monkeypatch.setattr("tools.files.seed_from_history", counting)
    _ = await call_files(caps, "edit_file", _edit(), conversation="c", messages=history)
    assert scans == []
    _ = await call_files(caps, "edit_file", _edit(), conversation="other", messages=history)
    assert scans == [1]


def test_seeding_never_overwrites_a_newer_record():
    ledger = ReadLedger()
    ledger.record("c", "/a", "bbbbbbbbbbbb")
    stale = _read_history("a", "[a | 1 lines | hash:aaaaaaaaaaaa]\nx")
    seed_from_history(ledger, "c", stale, lambda tool, args: "/a")
    assert ledger.get("c", "/a") == "bbbbbbbbbbbb"
    seed_from_history(ledger, "c", stale, lambda tool, args: "/b")
    assert ledger.get("c", "/b") == "aaaaaaaaaaaa"
