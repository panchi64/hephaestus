"""Turning a picked command into the block that rides this turn.

**The operator's literal text stays their turn.** ``/review the auth route`` is what
persists and what the transcript renders; what this module builds is appended to the
*tail* of the same turn's user prompt, announced as a context injection, and stripped
before the turn is recorded (``agent/prelude``, ``services/conversations``'s
``install_persisted_attachments``). Three consequences worth holding on to:

* A command body **never enters history**. It is re-derived each turn it applies to, so
  editing a skill changes what the next invocation does rather than leaving an old copy
  replayed forever.
* It never touches the request *head*, so the cached prefix survives a command that
  differs on every turn.
* The Auto reviewer reads position 0 of a user part and explicitly refuses to join the
  tail (``services/permissions/reviewer``), so nothing written here can wear the
  operator's label in a review. That is the property that makes it safe to put a
  project-declared template in this block at all.

**Skills and sub-agents are reached by directive, not by invocation.** Both already have
one tested path — ``skills_open`` and ``agents_spawn`` — carrying staging, the
published-only check, permission folding and the approval gate. A second path that opened
or launched from here would have to reproduce all of it, and in the sub-agent's case would
produce one with no parent turn to report into. So the block says what the operator asked
for and the model makes the call it would have made anyway.
"""

from __future__ import annotations

from .spec import CommandSpec

#: What the operator typed after the command name, when it matters to the directive. Kept
#: short: the argument is already in their prompt a line above, and this restates it only
#: where the model has to put it somewhere specific (a sub-agent's brief).
_NO_ARGUMENT = "They gave no further instructions — use the conversation so far."


def expand(spec: CommandSpec, argument: str) -> str:
    """The tail block for one invoked command, or ``""`` when it needs none."""
    argument = argument.strip()
    if spec.source == "skill":
        return _skill_block(spec)
    if spec.source == "agent":
        return _agent_block(spec, argument)
    if spec.body:
        return _template_block(spec, argument)
    return ""


def _skill_block(spec: CommandSpec) -> str:
    """Open the skill, then work from it.

    Deliberately *not* the skill's body inlined. ``skills_open`` stages the bundle's
    supporting files into the run's workspace as real files, which is most of what a skill
    is worth; a body pasted here would arrive without any of them. It is also classified a
    read, so the call costs no approval at any level.
    """
    return (
        f"The operator invoked the `{spec.target}` skill by name.\n"
        f"Call `skills_open(name={spec.target!r})` before doing anything else in this "
        "turn, then follow the instructions it returns. Do not ask whether to use it — "
        "they have already said so."
    )


def _agent_block(spec: CommandSpec, argument: str) -> str:
    """Launch the named sub-agent with what the operator wrote as its brief.

    The brief is restated here rather than left implicit in the prompt above, because it
    has to end up in one specific argument: a sub-agent starts from an empty history and
    never sees this conversation, so a task reading "do what they asked" describes nothing
    it can act on.
    """
    brief = f"Their brief, verbatim: {argument}" if argument else _NO_ARGUMENT
    return (
        f"The operator invoked the `{spec.target}` sub-agent by name.\n"
        f"Launch it with `agents_spawn(name=…, agent_name={spec.target!r}, …)`. {brief}\n"
        "Write the task so it stands alone — the sub-agent starts from an empty history "
        "and never sees this thread. Do not ask which sub-agent to use."
    )


def _template_block(spec: CommandSpec, argument: str) -> str:
    """A prompt template the operator wrote, or their project declared.

    The body is **delimited and capped, never wrapped as untrusted content.** The untrusted
    fence exists to tell the model not to follow what is inside it, and a template the
    operator deliberately invoked is precisely a set of instructions they want followed —
    fencing it would defeat the feature it is meant to protect. The bound that does apply
    is a byte cap at the point the file is read, the same one a project's agent briefs and
    its instruction files already live under.

    The delimiter earns its place separately: without it the injection row in the work log
    is indistinguishable from the operator's own words, which is the question that row
    exists to answer.
    """
    tail = f"\n\nWhat they added after the command: {argument}" if argument else ""
    return (
        f"The operator invoked the `{spec.name}` command. The template below is theirs — "
        "follow it for this turn.\n"
        f"--- begin {spec.name} ---\n"
        f"{spec.body}\n"
        f"--- end {spec.name} ---{tail}"
    )
