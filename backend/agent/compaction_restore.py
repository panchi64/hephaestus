"""What a fold puts back after the briefing: the files the thread was working in, its live
plan and the agents it spawned that are still working, read fresh, so the model resumes
from their current state rather than a paraphrase.

Attached to the checkpoint as one more user part, framed as the workspace's. The files are
the ones the folded history last read or wrote through the file tools, or restored at the
previous fold, so they carry over from fold to fold.

The task list needs no restoring: it rides every turn's prompt tail (``tools/tasks.py``).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from pydantic_ai import ModelMessage, ToolCallPart, ToolReturnPart, UserPromptPart

from core.config import Settings
from core.text import CHARS_PER_TOKEN_PROSE, chars_to_tokens, clip
from core.untrusted import neutralize_tags
from services.plan_mode import PlanMode
from services.subagents import SubagentLauncher, SubagentView
from tools import RunDeps
from tools.files import read_again
from tools.plan import render_plan

logger = logging.getLogger(__name__)

#: A plan in one of these states is still the thread's to carry out or revise.
_LIVE_PLAN = frozenset({"pending", "approved", "revising"})
_TAGS = ("restored-file", "restored-plan", "running-agents", "workspace-note")
_FILE_TOOLS = frozenset({"files_read_file", "files_write_file", "files_edit_file"})
_RESTORED_PATH = re.compile(r'<restored-file path="([^"]*)">')
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


def recent_files(messages: Sequence[ModelMessage]) -> list[str]:
    """The paths ``messages`` read or wrote through the file tools, or restored at an
    earlier fold, newest first and each once, as the calls spelled them."""
    calls: dict[str, str] = {}
    touched: list[str] = []
    for message in messages:
        for part in message.parts:
            if isinstance(part, ToolCallPart) and part.tool_name in _FILE_TOOLS:
                path = part.args_as_dict().get("path")
                if isinstance(path, str):
                    calls[part.tool_call_id] = path
            elif isinstance(part, ToolReturnPart) and part.outcome == "success":
                if part.tool_call_id in calls:
                    touched.append(calls[part.tool_call_id])
            elif isinstance(part, UserPromptPart) and isinstance(part.content, str):
                found: list[str] = _RESTORED_PATH.findall(part.content)
                touched.extend(path.replace("%22", '"') for path in found)
    return list(dict.fromkeys(reversed(touched)))


async def restore_after_fold(
    deps: RunDeps, settings: Settings, messages: Sequence[ModelMessage]
) -> Restored:
    """Re-read the files ``messages`` (the history being folded) last touched and the
    thread's live plan, within the settings' budgets."""
    blocks: list[str] = []
    names: list[str] = []
    hosts: set[str] = set()
    spent = 0.0
    per_file = int(settings.compaction_restore_file_tokens * CHARS_PER_TOKEN_PROSE)
    for path in recent_files(messages):
        if len(names) >= settings.compaction_restore_files:
            break
        read = await read_again(deps, path, max_chars=per_file)
        if read is None or read.host in hosts:
            continue
        cost = chars_to_tokens(len(read.text))
        if spent + cost > settings.compaction_restore_total_tokens:
            break
        spent += cost
        hosts.add(read.host)
        names.append(read.path)
        safe_path = read.path.replace('"', "%22")
        body = neutralize_tags(read.text, _TAGS)
        blocks.append(f'<restored-file path="{safe_path}">\n{body}\n</restored-file>')
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
