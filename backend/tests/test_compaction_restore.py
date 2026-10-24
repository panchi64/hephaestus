"""After a fold the thread's recent files and live plan come back, and nothing else is
editable without a fresh read."""

from __future__ import annotations

from pathlib import Path

from pydantic_ai import ModelRequest, ModelResponse, TextPart, UserPromptPart

from agent.compaction_restore import RESTORED_PLAN, Restored, restore_after_fold
from core.config import Settings
from core.container import ServiceContainer
from prompts.compaction import COMPACT_CLOSE, COMPACT_PREAMBLE
from routes.deps import OPERATOR_ID, app_state

from ._helpers import client_app
from ._json import arr, json_body, obj
from .files_support import README, call_files, project_caps, run_deps

NOTES = "~/projects/api/NOTES.md"


def _names(restored: Restored) -> list[str]:
    """The restored names by file name: the ledger keeps the home path in canonical form."""
    return [name.rsplit("/", 1)[-1] for name in restored.names]


async def _two_files(tmp_path: Path) -> tuple[ServiceContainer, Path]:
    caps, root = await project_caps(tmp_path)
    _ = (root / "NOTES.md").write_text("the notes\n")
    _ = await call_files(caps, "read_file", {"path": README})
    _ = await call_files(caps, "read_file", {"path": NOTES})
    return caps, root


def _text(restored: Restored) -> str:
    assert restored.part is not None
    content = restored.part.content
    assert isinstance(content, str)
    return content


async def test_the_most_recent_files_come_back_newest_first(tmp_path: Path):
    caps, _ = await _two_files(tmp_path)
    restored = await restore_after_fold(run_deps(caps), Settings())
    assert _names(restored) == ["NOTES.md", "README.md"]
    text = _text(restored)
    assert text.startswith("<workspace-note>")
    assert text.index("the notes") < text.index("the api")
    assert f'<restored-file path="{restored.names[0]}">' in text


async def test_a_file_that_did_not_come_back_needs_a_fresh_read(tmp_path: Path):
    caps, root = await _two_files(tmp_path)
    restored = await restore_after_fold(run_deps(caps), Settings(compaction_restore_files=1))
    assert _names(restored) == ["NOTES.md"]
    refused = await call_files(
        caps, "edit_file", {"path": README, "old_text": "the api", "new_text": "x"}
    )
    assert "before changing it" in refused
    done = await call_files(
        caps, "edit_file", {"path": NOTES, "old_text": "the notes", "new_text": "x"}
    )
    assert done.startswith("Edited")
    assert (root / "README.md").read_text() == "the api\n"


async def test_the_total_budget_stops_the_restore(tmp_path: Path):
    caps, _ = await _two_files(tmp_path)
    restored = await restore_after_fold(run_deps(caps), Settings(compaction_restore_total_tokens=1))
    assert restored.part is None
    assert restored.names == []


async def test_a_deleted_file_is_skipped(tmp_path: Path):
    caps, root = await _two_files(tmp_path)
    (root / "NOTES.md").unlink()
    restored = await restore_after_fold(run_deps(caps), Settings())
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
        restored = await restore_after_fold(run_deps(caps, cid), Settings())
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
    restored = await restore_after_fold(run_deps(ServiceContainer()), Settings())
    assert restored.part is None
    assert restored.names == []
