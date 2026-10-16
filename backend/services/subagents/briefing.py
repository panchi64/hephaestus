"""What every sub-agent is told, on top of whatever its own spec says.

The standing part is true of *being* a sub-agent, so a roster entry that had to restate it
would be a roster entry that could get it wrong: the class it belongs to, how it reaches the
other agents of its tree, and the report it ends with. It is delivered as instructions rather
than as the opening message, rebuilt each turn and never read back out of history, so
nothing a sub-agent later reads can rewrite the terms it works under.

The workspace note is the one part that varies per *launch* rather than per sub-agent: the
same worker shares the launching thread's files, or works in a worktree of its own when the
thread asks for one. A brief that claims a private branch while the sub-agent is editing the
operator's own tree is the failure resolving it per launch avoids.
"""

from __future__ import annotations

from prompts.voice import CLASSES, REPORT_SHAPE, join
from services.agents.definition import WorkspacePolicy
from services.workspace import DELEGATION_SEP

_CONTRACT = """\
Another agent of the forge spawned you to do one self-contained piece of work and is \
waiting on your report. Usually nobody is in this thread with you. Where the rules above \
speak of keeping the operator posted or asking them a question, the agent that spawned you \
stands in: you post no updates, and you ask through a `blocked` report.

You belong to that agent's delegation tree. `agents_message` reaches the agent that spawned \
you (`parent`) and any other agent in the tree by its handle, which `agents_list` names. Use \
it when another agent needs something from you before you finish, never for your report. \
Messages from them arrive marked as such.

The operator can see what you do and is asked to approve anything that reaches past what \
you were allowed, so an act that needs their permission is worth attempting rather than \
working around.

End your work by calling `subagent_report`, every time, as your last action:
- `done` when the task is finished.
- `blocked` when you need an answer or a decision that is not yours to make. Say exactly \
what; the agent that spawned you answers, and its reply wakes you where you stopped.
- `failed` when you tried the safe routes and the task cannot be done as briefed. Never \
force it with something rash. Say what broke and what you tried.
Then end your turn without writing anything else. Your report is the only thing that \
survives you: nothing else from this thread reaches the agent reading it."""

_SHARED = """\
You work in the same workspace as the agent that spawned you. What you change is simply \
changed: there is nothing to merge and nothing to hand over, and an edit you did not mean to \
make is immediately somebody else's problem. That agent may be working in here at the same \
time, so don't revert or tidy anything you were not asked to touch."""

_ISOLATED = """\
You work on a branch and worktree of your own under `~/worktrees`, cut from where the agent \
that spawned you works; your file and shell tools start there. Edit, run and check your \
work in it. Nothing you change reaches anyone else's files: your scratch is shared with that \
agent, but the project you change only in your worktree.

When you finish, the agent that spawned you reviews your branch, fixes what it needs to, and \
decides whether to merge it. Say in your report what you changed and why, how you checked \
it, and anything you left unfinished."""

_OWN = """\
You have a scratch of your own, which starts empty and belongs to nobody else. The projects \
are the ones the agent that spawned you works in, and you may write what it may. Anything \
you want it to know about your scratch has to be in your report."""

_NOTES: dict[WorkspacePolicy, str] = {
    "shared": _SHARED,
    "isolated": _ISOLATED,
    "own": _OWN,
}


def workspace_note(policy: WorkspacePolicy) -> str:
    """What this sub-agent is told about the files it is working on."""
    return _NOTES[policy]


def policy_of(workspace_key: str) -> WorkspacePolicy:
    """Which workspace a spawned thread's stored key names: the empty key is its own, a
    delegated key is a worktree of its own, and any other key is its parent's."""
    if not workspace_key:
        return "own"
    return "isolated" if DELEGATION_SEP in workspace_key else "shared"


def standing(agent_class: str) -> str:
    """What a sub-agent of ``agent_class`` is told before its spec: its class, how it
    finishes, and how its report reads. The forge and the house rules are in its system
    prompt (``prompts.agent.SPAWNED_SYSTEM_PROMPT``)."""
    spec = CLASSES.get(agent_class, CLASSES["tripodes"])
    return join(spec.lore, _CONTRACT, REPORT_SHAPE)


def brief_for(spec_brief: str, policy: WorkspacePolicy, agent_class: str) -> str:
    """One sub-agent's whole standing brief, in the order it should be read: what it is,
    what it is *for*, and where it is working."""
    return join(standing(agent_class), spec_brief, workspace_note(policy))
