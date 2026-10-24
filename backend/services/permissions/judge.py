"""The deterministic stage: the free answer, and the shape of it.

Most Auto-level work is ordinary work inside the given directory (`git status`, `ls src`,
`uv run pytest`). This stage rules on structure first, and only what it declines reaches
the reviewer model.

**It has exactly one power: to approve.** What it does not recognise is escalated, never
refused. Narrowing it costs model calls; widening it costs the operator's trust, and only
the first is recoverable.

**There is no list of programs here.** An allowlist answers "what will this binary do",
which nobody in this repository maintains (`git diff` runs whatever `diff.external` names).
The stage rules on two facts it can establish:

- **structure**: the paths, redirects, environment assignments and unreadable constructs
  the grammar finds (``shell_ast.py``);
- **a fence**: the conversation VM (``services/vm/machines.py``) holds a command to the
  folders it mounts, and counts as a fence here only while it has no network (offline
  mode, or nothing allowed or granted). With egress, an unread program could upload what
  it reads to an allowed host, so nothing in the VM clears without review. The host tool
  (``shell_run_on_host``) is never cleared here.

**Reads of the project are the exception and structure carries the whole weight**: the VM
mounts the project live, so nothing downstream catches a read cleared here. Hence the
extraction refuses a word it cannot place as a single path (``shell_ast.py``), and a path
that escapes is refused before anything else is asked.

**"Relative to the workspace root" has to stay true.** A shell session persists its working
directory, so a relative path is judged against the root. Otherwise every containment
answer below is measured against a directory the command is no longer in.

**Three kinds of act clear here, each on its own ground** (:data:`Tier` records which):

- a **classified read** (``ActionKind.READ``): the class says the tool returns something and
  changes nothing, so no argument set makes it another kind of act. Only a *classified*
  name reaches it: an unknown one resolves to the class that reaches furthest, so nothing
  an operator's MCP server names can arrive wearing a read's clothes;
- an **offline sandbox call**: a program in scratch, with scratch the only writable place
  and a VM without network around it, needs no reading. This is the one branch that clears
  a capability the extraction marked unbounded;
- a **contained command**: a VM shell command with every path inside the worktree, no
  network address or fetch, in a VM without network.

**A networked command is not a fourth, deliberately.** The VM bounds writes and egress but
not reads of the project, so a command that reaches out is the only way read data leaves
the machine. An allowed-domains list does not make that safe: allowed hosts accept uploads
as readily as downloads (`git push`). So every networked command goes to the reviewer; the
allowlist stays what the VM holds an approved one to.

**Everything else escalates, and the reason names the missing fact**: an unreadable
construct, a path outside the worktree, the host tool, no VM, or a reach into the network.
The operator reads that reason on the review row.

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
    """Whether ``capability``'s structure and fence allow clearing it, and at which tier.

    ``fenced`` says whether a conversation VM with no network would hold the call
    (``agent/gating.py:vm_fences``). It is an argument so this stays pure and total, and
    never approves anything it did not fully account for.

    The class-settled tiers are tested before ``unbounded``, because a classified read is
    settled by its tool and a sandboxed call by its sandbox, not by their arguments. Every
    other branch reads the extraction, so a partially-read command is refused as one first.
    """
    if capability.kind is ActionKind.READ:
        # No model and no argument rule took part; the row should not read like a command's.
        return Judgement(True, "classified read, cleared at Auto with no review", tier="read")
    if capability.sandboxed and not capability.network and fenced:
        return Judgement(True, "runs offline in the conversation's own sandbox", tier="sandbox")
    if capability.unbounded:
        return Judgement(False, capability.unbounded[0])
    if capability.kind is not ActionKind.SHELL:
        return Judgement(False, "only reads and shell commands can be cleared without a review")
    if not capability.commands:
        return Judgement(False, "runs no command this stage can name")
    return _shell_judgement(capability, fenced=fenced)


def _shell_judgement(capability: Capability, *, fenced: bool) -> Judgement:
    """A fully-read shell command against what its structure names.

    Checks run from the fact that settles most cases to the one that settles fewest, so the
    reason is the most specific true one: where it runs, then what it names, then whether
    the conversation VM is up to hold it.

    Environment assignments and redirects decide nothing here: `LD_PRELOAD=x ls` is held
    to the same paths and egress as `ls`, and enumerating the variables that change a
    program is a losing game the fence makes unnecessary.
    """
    if capability.on_host:
        return Judgement(False, "runs on the operator's own machine, outside the conversation VM")
    if capability.escapes:
        return Judgement(False, f"names {capability.escapes[0]}, outside the workspace")
    if not fenced:
        return Judgement(False, "the conversation VM is down or can reach the network")
    if capability.network or capability.fetches:
        # See the module docstring: reaching out is the only way project reads leave.
        what = "names a network address" if capability.network else "installs or clones"
        return Judgement(False, f"{what}; the reviewer decides")
    return Judgement(True, "stays inside the worktree and reaches no network", tier="workspace")
