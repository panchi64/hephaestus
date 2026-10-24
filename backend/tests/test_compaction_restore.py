"""After a fold the files the folded history last touched and the live plan come back."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic_ai import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from agent.compaction_restore import RESTORED_PLAN, Restored, recent_files, restore_after_fold
from core.config import Settings
from core.container import ServiceContainer
from prompts.compaction import COMPACT_CLOSE, COMPACT_PREAMBLE
from routes.deps import OPERATOR_ID, app_state

from ._helpers import client_app
from ._json import arr, json_body, obj
from .files_support import README, project_caps, run_deps

NOTES = "~/projects/api/NOTES.md"


def _names(restored: Restored) -> list[str]:
    return [name.rsplit("/", 1)[-1] for name in restored.names]


def _touched(
    *calls: tuple[str, str], outcome: Literal["success", "failed"] = "success"
) -> list[ModelMessage]:
    """A history in which each ``(tool, path)`` call ran, oldest first."""
    messages: list[ModelMessage] = []
    for index, (tool, path) in enumerate(calls):
        cid = f"c{index}"
        messages.append(
            ModelResponse(parts=[ToolCallPart(f"files_{tool}", {"path": path}, tool_call_id=cid)])
        )
        messages.append(
            ModelRequest(
                parts=[ToolReturnPart(f"files_{tool}", "ok", tool_call_id=cid, outcome=outcome)]
            )
        )
    return messages


READS = _touched(("read_file", README), ("read_file", NOTES))


async def _two_files(tmp_path: Path) -> tuple[ServiceContainer, Path]:
    caps, root = await project_caps(tmp_path)
    _ = (root / "NOTES.md").write_text("the notes\n")
    return caps, root


def test_recent_files_are_newest_first_and_once():
    history = _touched(("read_file", README), ("edit_file", NOTES), ("read_file", README))
    assert recent_files(history) == [README, NOTES]


def test_a_failed_call_touched_nothing():
    assert recent_files(_touched(("read_file", README), outcome="failed")) == []


def test_a_previous_folds_files_carry_over():
    checkpoint = ModelRequest(parts=[UserPromptPart(f'<restored-file path="{NOTES}">\nx\n')])
    assert recent_files([checkpoint, *_touched(("read_file", README))]) == [README, NOTES]


def _text(restored: Restored) -> str:
    assert restored.part is not None
    content = restored.part.content
    assert isinstance(content, str)
    return content


async def test_the_most_recent_files_come_back_newest_first(tmp_path: Path):
    caps, _ = await _two_files(tmp_path)
    restored = await restore_after_fold(run_deps(caps), Settings(), READS)
    assert _names(restored) == ["NOTES.md", "README.md"]
    text = _text(restored)
    assert text.startswith("<workspace-note>")
    assert text.index("the notes") < text.index("the api")
    assert f'<restored-file path="{restored.names[0]}">' in text


async def test_the_file_count_caps_the_restore(tmp_path: Path):
    caps, _ = await _two_files(tmp_path)
    settings = Settings(compaction_restore_files=1)
    restored = await restore_after_fold(run_deps(caps), settings, READS)
    assert _names(restored) == ["NOTES.md"]


async def test_the_total_budget_stops_the_restore(tmp_path: Path):
    caps, _ = await _two_files(tmp_path)
    settings = Settings(compaction_restore_total_tokens=1)
    restored = await restore_after_fold(run_deps(caps), settings, READS)
    assert restored.part is None
    assert restored.names == []


async def test_a_deleted_file_is_skipped(tmp_path: Path):
    caps, root = await _two_files(tmp_path)
    (root / "NOTES.md").unlink()
    restored = await restore_after_fold(run_deps(caps), Settings(), READS)
    assert _names(restored) == ["README.md"]


async def test_a_pending_plan_comes_back():
    async with client_app() as (_client, app):
        state = app_state(app)
        cid = await state.conversations.create_conversation(OPERATOR_ID)
        _ = await state.plan_mode.submit(
            OPERATOR_ID, cid, title="Rewrite the parser", body="It is slow.", steps=["read it"]
        )
        caps = ServiceContainer()
        caps.add(state.plan_mode)
        restored = await restore_after_fold(run_deps(caps, cid), Settings(), [])
        assert restored.names == [RESTORED_PLAN]
        text = _text(restored)
        assert "<restored-plan>" in text
        assert "# Rewrite the parser" in text


async def test_a_reload_lists_what_came_back():
    """The cold read carries the restored list the live event did."""
    async with client_app() as (client, app):
        store = app_state(app).conversations
        cid = await store.create_conversation(OPERATOR_ID)
        _ = store.record(
            cid,
            [
                ModelRequest(parts=[UserPromptPart(content="hi")]),
                ModelResponse(parts=[TextPart(content="hello")]),
            ],
        )
        plan = await store.compaction_plan(cid)
        assert plan is not None
        _ = store.record_compaction(
            cid,
            summary=f"{COMPACT_PREAMBLE}\n\n## Goal\ng\n{COMPACT_CLOSE}",
            through_id=plan.through_id,
            expected_leaf_id=plan.expected_leaf_id,
            reason="manual",
            restored=UserPromptPart(content="<workspace-note>re-read</workspace-note>"),
            restored_names=["~/a.md", RESTORED_PLAN],
        )
        detail = json_body(await client.get(f"/conversations/{cid}"))
        divider = next(m for m in arr(detail["messages"]) if obj(m)["role"] == "compaction")
        assert obj(divider)["restored"] == ["~/a.md", RESTORED_PLAN]


async def test_nothing_to_restore_attaches_nothing():
    restored = await restore_after_fold(run_deps(ServiceContainer()), Settings(), READS)
    assert restored.part is None
    assert restored.names == []
