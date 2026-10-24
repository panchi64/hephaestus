"""What a fold puts back after the briefing: the files the thread was working in and its
live plan, read fresh, so the model resumes from their text rather than a paraphrase.

Attached to the checkpoint as one more user part, framed as the workspace's. The read
ledger is then reset to exactly the files restored: any other file the thread read before
the fold now needs reading again before an edit, since the model holds only a summary of
it.

The task list needs no restoring: it rides every turn's prompt tail (``tools/tasks.py``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from pydantic_ai import UserPromptPart

from core.config import Settings
from core.text import CHARS_PER_TOKEN_PROSE, chars_to_tokens
from core.untrusted import neutralize_tags
from services.plan_mode import PlanMode
from tools import RunDeps
from tools.files import read_again
from tools.plan import render_plan
from tools.read_ledger import Seen, ledger_of, thread_of

#: A plan in one of these states is still the thread's to carry out or revise.
_LIVE_PLAN = frozenset({"pending", "approved", "revising"})
_TAGS = ("restored-file", "restored-plan", "workspace-note")
#: Named in the restored list when the plan came back, beside the file paths.
RESTORED_PLAN = "plan"


@dataclass(frozen=True)
class Restored:
    """The part to attach to the checkpoint, and what it holds for the divider."""

    part: UserPromptPart | None = None
    names: list[str] = field(default_factory=list)


async def restore_after_fold(deps: RunDeps, settings: Settings) -> Restored:
    """Re-read the thread's most recent files and its live plan, within the settings'
    budgets, and reset the read ledger to the files that came back."""
    blocks: list[str] = []
    names: list[str] = []
    kept: dict[str, Seen] = {}
    ledger = ledger_of(deps.caps)
    thread = thread_of(deps)
    if ledger is not None:
        spent = 0.0
        per_file = int(settings.compaction_restore_file_tokens * CHARS_PER_TOKEN_PROSE)
        for path in ledger.recent(thread, settings.compaction_restore_files):
            read = await read_again(deps, path, max_chars=per_file)
            if read is None:
                continue
            cost = chars_to_tokens(len(read.text))
            if spent + cost > settings.compaction_restore_total_tokens:
                break
            spent += cost
            kept[read.host] = Seen(read.content_hash, read.path)
            names.append(read.path)
            safe_path = read.path.replace('"', "%22")
            body = neutralize_tags(read.text, _TAGS)
            blocks.append(f'<restored-file path="{safe_path}">\n{body}\n</restored-file>')
        ledger.reset(thread, dict(reversed(kept.items())))
    plan_text = await _live_plan(deps)
    if plan_text is not None:
        names.append(RESTORED_PLAN)
        blocks.append(f"<restored-plan>\n{neutralize_tags(plan_text, _TAGS)}\n</restored-plan>")
    if not blocks:
        return Restored()
    at = datetime.now(UTC).isoformat(timespec="minutes")
    note = (
        "<workspace-note>From this workspace, not from the operator: re-read after the "
        f"conversation was compacted, as of {at}.</workspace-note>"
    )
    return Restored(part=UserPromptPart("\n\n".join([note, *blocks])), names=names)


async def _live_plan(deps: RunDeps) -> str | None:
    plans = deps.caps.get_optional(PlanMode)
    if plans is None or deps.conversation_id is None:
        return None
    plan = await plans.current(deps.run.owner_id, deps.conversation_id)
    if plan is None or plan.status not in _LIVE_PLAN:
        return None
    return render_plan(plan)
