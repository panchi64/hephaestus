"""The deterministic stage — the answer that costs nothing, and the shape of it.

Most of what an agent does at the Auto level is ordinary work inside the directory it was
given: `git status`, `ls src`, `uv run pytest`, `mkdir -p a/b`, `git commit -m wip`.
Sending each of those to a model costs a round trip, a second of latency and a chance of
being wrong about something structure already settles. So the first stage rules
structurally, and only what it declines reaches the reviewer.

**It has exactly one power: to approve.** A capability this stage does not recognise is
not refused — it is *escalated*, which is a different thing and the reason this file can
afford to be as strict as it is. Narrowing it costs model calls; widening it costs the
operator's trust, and only one of those is recoverable.

**There is no list of programs here, and that is the change this file exists to state.**
An allowlist of read-only programs answers "what will this binary do", which is a question
about software nobody in this repository maintains — every row was a claim someone had to
re-check against a man page, and the rows that mattered (`git diff` running whatever
`diff.external` names) could not be written at all. The question this stage asks now is
answerable from three facts it can actually establish:

- **structure** — what the command's syntax names, read off a grammar (``shell_ast.py``):
  which paths, which redirects, which environment assignments, and every construct that
  could not be read at all;
- **a declared reach** — ``workspace``, ``network`` or ``host``, stated by the model on the
  call itself (:data:`~services.permissions.capability.Reach`);
- **a fence** — whether a process can actually be *held* to that reach: a shell command
  runs in the platform VM in the agent's home (``services/sandbox/home_shell.py``), with
  no network unless it declared one.

The declaration is what makes the structure decidable and the fence is what holds a command
to most of it: a command declaring ``workspace`` runs in the VM with no network, so an
egress beyond what it declared fails *inside* the fence instead of quietly succeeding, and
only the project's own files come back out. **Reads of the project are the exception, and
the one place structure carries the whole weight**: the guest copy holds the project, so
nothing downstream will catch a read of it that this stage cleared. That is why the
extraction refuses a word it cannot place as a single path (``shell_ast.py``) rather than
measuring it optimistically, and why a path that escapes is refused here before anything
else is asked.

**And it is why "relative to the workspace root" has to stay true.** A shell session
persists its working directory between calls, so a relative path is judged as relative to
the root — an assumption that is load-bearing for every command cleared here, and that
only the shell runner can keep: it adopts the directory a command left the
shell in **only while that is still under the root**
(``services/sandbox/shell_runner.py``). Let
the tracked directory out of the worktree and every containment answer below is measured
against a directory the command is no longer in.

This stage's job is to check that the three agree — and to refuse whenever they do not, or
whenever one of them is missing.

**Three kinds of act clear here, and each on its own ground** (:data:`Tier` records which):

- a **classified read** (``ActionKind.READ``): the class is a claim about the tool itself —
  it returns something and leaves nothing different behind — so no argument set makes it
  into another kind of act and there is no reviewer question left to ask. This is what
  keeps a self-gated recall (``platform_search``) from costing a
  model call every time, and what stops it from parking a run outright on an installation
  with no utility model bound. Only a *classified* name reaches it: an unknown one resolves
  to the class that reaches furthest, so nothing an operator's own MCP server names can
  arrive wearing a read's clothes;
- an **offline sandbox call**: the conversation's sandbox is itself the fence, so the
  program inside it needs no reading. This is the one branch that clears a capability the
  extraction marked unbounded, and deliberately: for an interpreter there is no reading of
  the arguments that would ever bound it, and the boundary was never the arguments;
- a **contained command** at ``workspace`` — every path it names inside the worktree, no
  network, and a fence available to hold it there.

**A ``network`` declaration is not a fourth one, and the asymmetry is deliberate.** The
fence bounds writes and egress and cannot bound *reads* of the project, so a command
cleared at ``workspace`` can already read everything in it — and the only way that leaves
the machine is a command that reaches out. An allowed-domains list does not make
that safe: the seed names hosts that accept uploads as readily as they serve downloads, so
`git push` of copied data is inside it. So every networked command goes to the model
reviewer, whatever the operator has allowed, and the allowed list stays what the *fence*
holds an approved one to (``tools/shell.py``) rather than what clears it.

**Everything else escalates, and the reason says which of the three facts was missing** —
an unreadable construct, a path outside the worktree, a `host` declaration, a declaration
the command's own syntax contradicts, a command with no fence to hold it, or a reach
into the network. That reason is what an operator reads on the review row, and "the
system was arbitrary" is the reading it exists to prevent.

**Pure, and pure on purpose.** Nothing here probes the host or reads settings: whether a
fence exists arrives as an argument, so the same call is decidable in a test, at the gate
(``agent/gating.py``) and again inside the tool that executes it (``tools/shell.py``) —
and those three cannot drift into disagreeing about what was cleared.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from services.permissions.capability import ActionKind, Capability

#: Which deterministic ground cleared a capability. Not a degree of trust and not an
#: ordering: they are three different arguments, and the one granted to a classified read
#: grants nothing to a command. It travels onto the review row and into the tool that
#: executes the call, which reads it to decide *which fence* to run the command under —
#: so a tier is a decision something else acts on, not a label.
type Tier = Literal["read", "sandbox", "workspace"]


@dataclass(frozen=True)
class Judgement:
    """The deterministic stage's answer about one capability.

    ``approved`` is the only field a caller has to branch on. ``reason`` exists for the
    other half of the job — an operator watching a benign-looking command escalate needs to
    be told *which* part of it this stage would not vouch for. ``tier`` is the same answer
    in a form something else can rule on (see :data:`Tier`).
    """

    approved: bool
    reason: str
    tier: Tier | None = None


def judge(capability: Capability, *, fenced: bool) -> Judgement:
    """Whether ``capability``'s structure, declaration and fence agree — and at which tier.

    ``fenced`` says whether a process can be held to its declared reach at all — whether
    it runs in the platform VM rather than on the host. It is a fact about the machine
    rather than about the call, which is why it is an argument: this function stays pure
    and total, never raises, never calls out, and never returns ``approved`` for anything
    it did not fully account for.

    The two class-settled tiers are tested first, ahead of ``unbounded``, because for both
    of them the arguments are not what settles the act: a classified read is settled by its
    tool and a sandboxed call by its sandbox. Every other branch reads the extraction, so
    a partially-read command is refused *as* one before any of them.
    """
    if capability.kind is ActionKind.READ:
        # Named for what it is on the row the operator reads: a tool whose *class* says it
        # observes, cleared on that class alone. It is the one approval here that no model
        # and no rule about arguments took part in, and the row should not let that pass
        # for the same kind of answer a command gets.
        return Judgement(True, "classified read, cleared at Auto with no review", tier="read")
    if capability.sandboxed and not capability.network:
        # The sandbox is the fence: no host filesystem, no egress, and outputs that come
        # back explicitly. Reading the program would add nothing, which is why this is the
        # one branch that clears something the extraction could not bound.
        return Judgement(True, "runs offline in the conversation's own sandbox", tier="sandbox")
    if capability.unbounded:
        return Judgement(False, capability.unbounded[0])
    if capability.kind is not ActionKind.SHELL:
        return Judgement(False, "only reads and shell commands can be cleared without a review")
    if not capability.commands:
        return Judgement(False, "runs no command this stage can name")
    return _shell_judgement(capability, fenced=fenced)


def _shell_judgement(capability: Capability, *, fenced: bool) -> Judgement:
    """A fully-read shell command against the reach it declared.

    The order is from the fact that settles most cases to the fact that settles fewest, so
    the reason an operator reads is the most specific true one: what the command *said* it
    needs comes before what it names, and both come before whether the host can hold it —
    a `host` declaration is refused as a declaration rather than as a missing fence.

    **Environment assignments and redirects no longer decide anything here.** `LD_PRELOAD=x
    ls` is a different program from `ls`, and a fence does not care: whatever it loads is
    held to the same paths and the same egress as the command that loaded it. Enumerating
    the variables that change what a program does was always a losing game, and the fence is
    what stopped it having to be won.
    """
    reach = capability.reach
    if reach is None:
        # A shell-shaped call from a tool with no `reach` argument. Nothing was declared, so
        # there is no declaration to check the structure against and no tier to build a
        # fence from; saying it declared `host` would be this stage inventing the model's
        # words.
        return Judgement(False, "declares no reach, so there is nothing here to hold it to")
    if reach == "host":
        return Judgement(
            False, 'declared reach "host", which is the operator\'s own machine unfenced'
        )
    if capability.escapes:
        return Judgement(
            False,
            f'declared reach "{reach}" but names {capability.escapes[0]}, outside the workspace',
        )
    if reach == "network":
        # The one declaration that is never settled here, and not because it is unreadable:
        # the fence does not bound reads of the project, so a command cleared for the worktree
        # can read all of it, and reaching out is the only way any of it leaves. An
        # allowed-domains list narrows where it may go and not what it may send, so who
        # approves an egress stays a judgement about the conversation.
        return Judgement(False, "reaches the network; the reviewer decides")
    if not fenced:
        # Nothing about the command is wrong; there is simply nothing here that would hold
        # it to what it declared, so the declaration buys nothing and the model reviewer
        # rules with the structural facts in front of it instead.
        return Judgement(False, "no fence is available to hold it to that")
    if capability.network:
        # The declaration and the command's own syntax disagree, and the disagreement is
        # the finding: a fence built for `workspace` would deny the egress anyway, so what
        # this refusal buys is the operator being told rather than the model being puzzled.
        return Judgement(False, 'declared reach "workspace" but names a network address')
    return Judgement(True, "stays inside the worktree and reaches no network", tier="workspace")
