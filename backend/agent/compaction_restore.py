"""What a fold puts back after the briefing: the files the thread was working in, its live
plan and the agents it spawned that are still working, read fresh, so the model resumes
from their current state rather than a paraphrase.

Attached to the checkpoint as one more user part, framed as the workspace's. The read
ledger is then reset to exactly the files restored: any other file the thread read before
the fold now needs reading again before an edit, since the model holds only a summary of
it.

The task list needs no restoring: it rides every turn's prompt tail (``tools/tasks.py``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from pydantic_ai import UserPromptPart

from core.config import Settings
from core.text import CHARS_PER_TOKEN_PROSE, chars_to_tokens, clip
from core.untrusted import neutralize_tags
from services.plan_mode import PlanMode
from services.subagents import SubagentLauncher, SubagentView
from tools import RunDeps
from tools.files import read_again
from tools.plan import render_plan
from tools.read_ledger import Seen, ledger_of, thread_of

logger = logging.getLogger(__name__)

#: A plan in one of these states is still the thread's to carry out or revise.
_LIVE_PLAN = frozenset({"pending", "approved", "revising"})
_TAGS = ("restored-file", "restored-plan", "running-agents", "workspace-note")
#: Named in the restored list when the plan came back, beside the file paths.
RESTORED_PLAN = "plan"
_TASK_CHARS = 300
_STATUS_WORDS = {"blocked": "waiting for input"}
_RUNNING_AGENTS_NOTE = (
    "Still working when the conversation was compacted. Each one's report arrives as a "
    "message when it finishes; `agents_list` shows where they stand now."
)


@dataclass(frozen=True)
class Restored:
    """The part to attach to the checkpoint, and what it holds for the divider."""

    part: UserPromptPart | None = None
    names: list[str] = field(default_factory=list)
    #: The running-agents block alone, for the fold's context-injection notice.
    agents: str | None = None


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
    # Read last: the caller records the checkpoint with no await after this returns, so a
    # child that settles after this read settles after the checkpoint, and its report
    # lands behind it.
    agents = _render_agents(await _running_agents(deps))
    if agents is not None:
        blocks.append(agents.block)
        names.extend(agents.names)
    if not blocks:
        return Restored()
    at = datetime.now(UTC).isoformat(timespec="minutes")
    note = (
        "<workspace-note>From this workspace, not from the operator: re-read after the "
        f"conversation was compacted, as of {at}.</workspace-note>"
    )
    return Restored(
        part=UserPromptPart("\n\n".join([note, *blocks])),
        names=names,
        agents=None if agents is None else agents.block,
    )


@dataclass(frozen=True)
class _AgentsBlock:
    block: str
    names: list[str]


async def _running_agents(deps: RunDeps) -> list[SubagentView]:
    launcher = deps.caps.get_optional(SubagentLauncher)
    if launcher is None or deps.conversation_id is None:
        return []
    try:
        return await launcher.working_children(deps.run.owner_id, deps.conversation_id)
    except Exception:  # a fold must not fail over its notice of running agents
        logger.warning("could not list running agents for a fold", exc_info=True)
        return []


def _render_agents(agents: list[SubagentView]) -> _AgentsBlock | None:
    if not agents:
        return None
    lines = [_RUNNING_AGENTS_NOTE]
    for agent in agents:
        callsign = f" ({agent.callsign})" if agent.callsign else ""
        status = _STATUS_WORDS.get(agent.status, "running")
        task = neutralize_tags(clip(" ".join(agent.task.split()), _TASK_CHARS), _TAGS)
        lines.append(f"- `{agent.handle}`{callsign} — {status}: {task}")
    body = "\n".join(lines)
    return _AgentsBlock(
        block=f"<running-agents>\n{body}\n</running-agents>",
        names=[f"agent {agent.handle}" for agent in agents],
    )


async def _live_plan(deps: RunDeps) -> str | None:
    plans = deps.caps.get_optional(PlanMode)
    if plans is None or deps.conversation_id is None:
        return None
    plan = await plans.current(deps.run.owner_id, deps.conversation_id)
    if plan is None or plan.status not in _LIVE_PLAN:
        return None
    return render_plan(plan)
