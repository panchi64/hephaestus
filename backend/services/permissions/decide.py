"""What to do with a tool call the model wants to make: the level's rule, then Auto's.

The engine reaches here when Pydantic AI hands back a *deferred* call. Two things defer one:

- **The level.** Every tool past the level's ceiling is marked as needing approval
  (``tools/toolsets.py``), and the level's approval policy answers: refused under Plan,
  parked under Manual and Edit, settled by :func:`auto_decision` under Auto. Yolo's ceiling
  is above every class, so the level never defers anything of its.
- **The tool itself.** A global recall, a skill edit, an untrusted external tool gate their
  own calls for reasons the level knows nothing about. Those park at every level below Yolo
  except Auto, which settles them by the same rule, and Yolo allows them: the operator asked
  not to be asked (``levels.py``). Neither answers a plan approval (``OPERATOR_ONLY_TOOLS``),
  which raises the thread's level and so always parks.

``ALLOW`` has three producers: a standing conversation grant, Auto's rule, and Yolo. Only
the grant is revocable while a run sits parked, so ``agent/gating.py`` marks which produced
it and the resume re-checks the grant's (``routes/runs.py``). A grant answers a question
this module asked and never overturns a level's refusal.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from services.permissions.levels import (
    OPERATOR_ONLY_TOOLS,
    ApprovalPolicy,
    beyond_scope,
    permission_spec,
)
from services.tool_sensitivity import Sensitivity, classified, sensitivity_of


class Decision(StrEnum):
    """What happens to one deferred tool call."""

    #: Run it, with no operator round-trip.
    ALLOW = "allow"
    #: Settle it by Auto's rule (:func:`auto_decision`) into one of the other three.
    REVIEW = "review"
    #: Park the run and put the call in front of the operator.
    ASK = "ask"
    #: Refuse it and tell the model why (:func:`blocked_message`). Only a level refuses.
    BLOCK = "block"


#: Total over :class:`ApprovalPolicy`, though ``ALLOW``'s row is unreachable today: a
#: partial mapping would fail as a ``KeyError`` inside a permission decision.
_BY_POLICY = {
    ApprovalPolicy.WITHHOLD: Decision.BLOCK,
    ApprovalPolicy.ASK: Decision.ASK,
    ApprovalPolicy.REVIEW: Decision.REVIEW,
    ApprovalPolicy.ALLOW: Decision.ALLOW,
}


def decide(level: str, tool: str) -> Decision:
    """Rule on one deferred call to ``tool`` in a thread at ``level``.

    Pure and total: an unknown level resolves to the strictest one and an unclassified tool
    to the class that reaches furthest, so neither a corrupt stored value nor an operator's
    own MCP tool is waved through. :data:`OPERATOR_ONLY_TOOLS` always come back ``ASK``.
    """
    if tool in OPERATOR_ONLY_TOOLS:
        return Decision.ASK
    policy = permission_spec(level).approval_policy
    if not beyond_scope(level, tool):
        # Within the level's scope, so the tool gated its own call. Only Yolo waves that
        # through, and only Auto answers it by rule rather than by asking.
        match policy:
            case ApprovalPolicy.ALLOW:
                return Decision.ALLOW
            case ApprovalPolicy.REVIEW:
                return Decision.REVIEW
            case _:
                return Decision.ASK
    return _BY_POLICY[policy]


def blocked_message(level: str, tool: str) -> str:
    """What the model is told when a call is refused, in place of the tool's result.

    Names the level and that no prompt is coming, so the model re-plans instead of
    retrying a refusal it read as a transient failure.
    """
    return (
        f"This conversation is at the {permission_spec(level).level} permission level, so "
        f"{tool} was not run and the operator was not asked. Nothing here can change "
        "anything until they raise the level; say what you would do instead."
    )


#: The tools whose work runs in the conversation's VM, which bounds it while it is offline.
VM_TOOLS = frozenset({"shell_run_command", "shell_start_command", "code_execute"})
HOST_TOOLS = frozenset({"shell_run_on_host"})
#: The request to open the VM's network to more hosts.
EGRESS_TOOL = "code_request_egress"

#: The deferral metadata key a parked Auto call's reason is written under.
AUTO_KEY = "auto"


@dataclass(frozen=True)
class AutoEnv:
    """The facts about the conversation's VM that Auto's rule reads, taken once per batch."""

    vm_up: bool
    #: No host is reachable: no egress allowlist and no egress request in the batch.
    offline: bool
    #: The home mounts an operator's own checkout writable. Root in the guest can rewrite
    #: its `.git`, which their next host commit runs, so the VM is no fence there.
    writable_checkout: bool


@dataclass(frozen=True)
class AutoOutcome:
    """Auto's answer for one call, with the reason the operator reads on its card."""

    decision: Decision
    reason: str
    #: A standing grant produced the allow, so the resume re-checks it.
    by_grant: bool = False
    #: The offline VM produced the allow, so execution re-checks the VM is still offline.
    by_environment: bool = False


def auto_decision(tool: str, *, env: AutoEnv, granted: bool) -> AutoOutcome:
    """Settle one call at Auto: what the environment bounds runs, then what a standing
    grant covers runs, and everything else parks."""
    cleared, reason = environment_clears(tool, env)
    if cleared:
        return AutoOutcome(Decision.ALLOW, reason, by_environment=tool in VM_TOOLS)
    if granted:
        covered = f"{reason}; your standing grant covers it"
        return AutoOutcome(Decision.ALLOW, covered, by_grant=True)
    return AutoOutcome(Decision.ASK, reason)


def environment_clears(tool: str, env: AutoEnv) -> tuple[bool, str]:
    """Whether the environment alone bounds a call to ``tool``, and why or why not.

    A classified read changes nothing. VM work is bounded by the VM's mounts and its lack
    of network, so it clears exactly when the VM is up, offline, and holds no writable
    checkout. Only a classified name reads as a read: an unknown one resolves to the class
    that reaches furthest (``tool_sensitivity``).
    """
    if classified(tool) and sensitivity_of(tool) is Sensitivity.READ:
        return True, "only reads"
    if tool in HOST_TOOLS:
        return False, "runs on the host"
    if tool not in VM_TOOLS:
        return False, "acts outside the VM"
    if not env.vm_up:
        return False, "the VM is unavailable"
    if not env.offline:
        return False, "the VM can reach the network"
    if env.writable_checkout:
        return False, "this thread's own checkout is writable"
    return True, "runs in the offline VM"
