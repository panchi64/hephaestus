"""The deterministic stage: the free answer, and the shape of it.

Most Auto-level work is ordinary work inside the given directory (`git status`, `ls src`,
`uv run pytest`). This stage rules on structure first, and only what it declines reaches
the reviewer model.

**It has exactly one power: to approve.** What it does not recognise is escalated, never
refused. Narrowing it costs model calls; widening it costs the operator's trust, and only
the first is recoverable.

**There is no list of programs here.** An allowlist answers "what will this binary do",
which nobody in this repository maintains (`git diff` runs whatever `diff.external` names).
The stage rules on three facts it can establish:

- **structure**: the paths, redirects, environment assignments and unreadable constructs
  the grammar finds (``shell_ast.py``);
- **a declared reach**: ``offline``, ``network`` or ``host``, stated by the model on the
  call (:data:`~services.permissions.capability.Reach`);
- **a fence**: whether a process can be held to that reach. A shell command runs in the
  platform VM in the agent's home (``services/sandbox/home_shell.py``), with no network
  unless it declared one.

A command declaring ``offline`` runs in the VM with no network, so egress beyond the
declaration fails inside the fence and only the project's own files come back out.
**Reads of the project are the exception and structure carries the whole weight**: the
guest copy holds the project, so nothing downstream catches a read cleared here. Hence
the extraction refuses a word it cannot place as a single path (``shell_ast.py``), and a
path that escapes is refused before anything else is asked.

**"Relative to the workspace root" has to stay true.** A shell session persists its working
directory, so a relative path is judged against the root. Only the shell runner keeps that
true: it adopts the directory a command left the shell in only while that is still under
the root (``services/sandbox/host_shell.py``). Otherwise every containment answer below is
measured against a directory the command is no longer in.

**Three kinds of act clear here, each on its own ground** (:data:`Tier` records which):

- a **classified read** (``ActionKind.READ``): the class says the tool returns something and
  changes nothing, so no argument set makes it another kind of act. Only a *classified*
  name reaches it: an unknown one resolves to the class that reaches furthest, so nothing
  an operator's MCP server names can arrive wearing a read's clothes;
- an **offline sandbox call**: the conversation's sandbox is the fence, so the program
  inside needs no reading. This is the one branch that clears a capability the extraction
  marked unbounded, because no reading of an interpreter's arguments would bound it;
- a **contained command** at ``offline``: every path inside the worktree, no network, and a
  fence available to hold it there.

**A ``network`` declaration is not a fourth, deliberately.** The fence bounds writes and
egress but not reads of the project, so a command that reaches out is the only way read
data leaves the machine. An allowed-domains list does not make that safe: allowed hosts
accept uploads as readily as downloads (`git push`). So every networked command goes to the
reviewer; the allowed list stays what the *fence* holds an approved one to
(``tools/shell.py``).

**Everything else escalates, and the reason names the missing fact**: an unreadable
construct, a path outside the worktree, a `host` declaration, a declaration the syntax
contradicts, no fence, or a reach into the network. The operator reads that reason on the
review row.

**Pure on purpose.** Nothing here probes the host or reads settings; whether a fence exists
arrives as an argument, so a test, the gate (``agent/gating.py``) and the executing tool
(``tools/shell.py``) cannot disagree about what was cleared.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from services.permissions.capability import ActionKind, Capability

#: Which deterministic ground cleared a capability. Three different arguments, not an
#: ordering: a classified read's tier grants nothing to a command. The executing tool reads
#: it to choose which fence to run the command under.
type Tier = Literal["read", "sandbox", "workspace"]


@dataclass(frozen=True)
class Judgement:
    """The deterministic stage's answer about one capability.

    ``approved`` is the field callers branch on; ``reason`` tells the operator which part
    of an escalated command this stage would not vouch for; ``tier`` is the answer in a
    form other code can rule on (see :data:`Tier`).
    """

    approved: bool
    reason: str
    tier: Tier | None = None


def judge(capability: Capability, *, fenced: bool) -> Judgement:
    """Whether ``capability``'s structure, declaration and fence agree, and at which tier.

    ``fenced`` says whether the process runs in the platform VM, where it can be held to
    its declared reach. It is an argument so this stays pure and total, and never approves
    anything it did not fully account for.

    The class-settled tiers are tested before ``unbounded``, because a classified read is
    settled by its tool and a sandboxed call by its sandbox, not by their arguments. Every
    other branch reads the extraction, so a partially-read command is refused as one first.
    """
    if capability.kind is ActionKind.READ:
        # No model and no argument rule took part; the row should not read like a command's.
        return Judgement(True, "classified read, cleared at Auto with no review", tier="read")
    if (
        capability.sandboxed
        and not capability.network
        and (fenced or capability.kind is not ActionKind.SHELL)
    ):
        # A shell command is held in the sandbox only by the VM, so it needs the fence up.
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

    Checks run from the fact that settles most cases to the one that settles fewest, so the
    reason is the most specific true one: what the command said it needs, then what it
    names, then whether the host can hold it.

    Environment assignments and redirects decide nothing here: `LD_PRELOAD=x ls` is held
    to the same paths and egress as `ls`, and enumerating the variables that change a
    program is a losing game the fence makes unnecessary.
    """
    reach = capability.reach
    if reach is None:
        # Nothing was declared; calling it `host` would invent the model's words.
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
        # See the module docstring: reaching out is the only way project reads leave.
        return Judgement(False, "reaches the network; the reviewer decides")
    if not fenced:
        # Nothing is wrong with the command; nothing would hold it to its declaration.
        return Judgement(False, "no fence is available to hold it to that")
    if capability.network or capability.fetches:
        # The shell runs it with the network anyway (`tools/shell.py:boundary`).
        what = "names a network address" if capability.network else "installs or clones"
        return Judgement(False, f'declared reach "offline" but {what}')
    return Judgement(True, "stays inside the worktree and reaches no network", tier="workspace")
