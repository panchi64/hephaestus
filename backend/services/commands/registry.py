"""Every command this request may offer, and what a picked one resolves to.

The registry owns two questions and no others: *which names exist right now*, and *which
spec a typed name means*. What a resolved command then does to the turn is
``expand``'s; what an action does is the client's, over a relay it already has.

**Five sources, one shape.** What the installation ships (thread actions, the built-in
sub-agent roster), what the operator wrote (published skills, saved workflows), and what the
project declared (``.claude/commands/*.md``, ``.claude/agents/*.md``). None of them exists
for this menu's sake; each is something the platform already had, given a name that can be
typed.

**Availability is decided here, never in the picker.** Three things narrow the catalog and
all three are facts only the backend holds: a sub-agent command is pointless where
``subagents_launch`` has been withheld (the operator's own switch, offline mode —
``services/tool_policy`` unions them), an action that acts on a conversation is pointless
in a composer that has none, and a project-declared command is pointless outside the
project's files. A client that filtered any of these would be deciding, and it
would be deciding from a stale copy.

**A collision drops nothing.** Sources are merged by locality — what the installation
ships, then what the operator wrote, then what the project declared — but the loser stays
in the catalog carrying ``shadowed_by``, because a skill and a sub-agent both called
``reviewer`` are two real things and the picker's job is to show the operator that. Only
the *bare* name resolves to one of them; ``/skill:reviewer`` still reaches the other.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from services.skills import SkillStore
from services.subagents import BUILTIN, SubagentSpec, merged_roster
from services.subagents.definitions import project_specs as project_agent_specs
from services.workspace_kind import is_project_folder

from .actions import BUILTIN_ACTIONS, NEEDS_CONVERSATION
from .definitions import project_specs as project_command_specs
from .spec import CommandSpec
from .store import CommandStore

#: The tool a sub-agent command exists to ask for. Withheld — by the operator's switch, by
#: offline mode — and the command is an offer the turn cannot honour.
LAUNCH_TOOL = "subagents_launch"

#: How the picker groups what it is given, in the order the headings appear. The labels
#: live here rather than in the client for the same reason the filtering does: a source
#: added later should show up named, not as a blank heading in a build that has not shipped
#: yet.
GROUPS: tuple[tuple[str, str], ...] = (
    ("action", "This thread"),
    ("skill", "Skills"),
    ("agent", "Sub-agents"),
    ("workflow", "Workflows"),
    ("project", "Project"),
)


@dataclass(frozen=True)
class CatalogEntry:
    """One row of the catalog: a spec, plus what (if anything) outranks its bare name."""

    spec: CommandSpec
    #: The qualified name that wins ``/{spec.name}``, when it is not this one.
    shadowed_by: str | None = None


class CommandRegistry:
    """Assembles the catalog per request. Holds no state of its own."""

    def __init__(self, skills: SkillStore, commands: CommandStore) -> None:
        self._skills = skills
        self._commands = commands

    async def catalog(
        self,
        owner_id: str,
        *,
        workspace_kind: str,
        has_conversation: bool,
        disabled_tools: Iterable[str] = (),
        root: Path | None = None,
    ) -> tuple[CatalogEntry, ...]:
        """What the picker may show, already narrowed and already ranked.

        ``root`` is the checkout this thread is looking at, resolved by the caller
        (``routes/deps.composer_root``) because *which tree* is a question about worktrees
        and projects that a catalog has no business answering. Absent — an unfiled thread,
        a sandbox thread, a composer with no project yet — the two project-declared sources
        simply contribute nothing, which is the honest answer rather than a degraded one.
        """
        withheld = frozenset(disabled_tools)
        # Assembled in **tier order** — what this installation ships, then what the
        # operator wrote, then what the project declared — so that `_merge`'s tiebreak
        # between equals is also source order, and reading this list top to bottom is
        # reading the precedence rule. Actions and agents are both tier 0 and must stay
        # above skills and workflows, both tier 1, for that to remain true.
        specs: list[CommandSpec] = [
            *_actions(has_conversation=has_conversation),
            *_agent_specs(root, workspace_kind, withheld=withheld),
            *await self._skill_specs(owner_id),
            *await self._commands.specs(owner_id),
            *(project_command_specs(root) if root is not None else ()),
        ]
        return _merge([spec for spec in specs if _offered_in(spec, workspace_kind)])

    async def resolve(
        self,
        owner_id: str,
        name: str,
        *,
        workspace_kind: str,
        has_conversation: bool,
        disabled_tools: Iterable[str] = (),
        root: Path | None = None,
    ) -> CommandSpec | None:
        """The spec a typed name means, bare or qualified — or ``None``.

        Resolved against the *same* narrowed catalog the picker was served, so a command
        that is not on offer cannot be reached by typing its name anyway. That matters more
        than it looks: the client sends a name, and a name is the one part of this exchange
        the operator can author by hand.
        """
        entries = await self.catalog(
            owner_id,
            workspace_kind=workspace_kind,
            has_conversation=has_conversation,
            disabled_tools=disabled_tools,
            root=root,
        )
        for entry in entries:
            if entry.spec.qualified_name == name:
                return entry.spec
        for entry in entries:
            if entry.spec.name == name and entry.shadowed_by is None:
                return entry.spec
        return None

    async def _skill_specs(self, owner_id: str) -> list[CommandSpec]:
        """One command per **published** skill.

        ``catalog`` is the published-only view the model already reads, so a draft is no
        more invocable by hand than it is by the model. That keeps publish as the single
        trust boundary rather than adding a second, quieter one here.
        """
        return [
            CommandSpec(
                name=entry.name,
                source="skill",
                kind="prompt",
                title=entry.name,
                description=entry.description,
                target=entry.name,
                argument_hint="what to apply it to",
            )
            for entry in await self._skills.catalog(owner_id)
        ]


def _actions(*, has_conversation: bool) -> list[CommandSpec]:
    """The built-in actions this composer can actually perform."""
    return [
        spec
        for spec in BUILTIN_ACTIONS
        if has_conversation or spec.name not in NEEDS_CONVERSATION
    ]


def _agent_specs(
    root: Path | None, workspace_kind: str, *, withheld: frozenset[str]
) -> list[CommandSpec]:
    """One command per sub-agent on the roster — the built-ins, plus what the project says.

    Merged through ``merged_roster``, which is the roster's own locality rule rather than a
    second one written here: a project that declares ``reviewer`` replaces the built-in of
    that name, because a repository that has written down how *its* reviewer works knows
    something this installation cannot. That is also what ``tools/project_agents.run_roster``
    does at launch, so the name the picker offers and the sub-agent that actually starts are
    resolved the same way — which is the only reason it is safe for the command's block to
    name a sub-agent by name and stop there.

    **And it is why the project layer is gated on the same check the launcher makes.**
    ``run_roster`` reads a project's declarations only in a project folder; offering them in
    a sandbox thread would put a row in the menu whose launch resolves to nothing, and the
    operator would meet that as a failed tool call mid-turn rather than as an absent option.

    One residual, inherent rather than fixable here: in a worktree thread before its first
    turn, ``root`` is still the operator's own checkout, so an agent file they have not
    committed is offered and will not be in the worktree the launcher resolves against. The
    command carries only the *name*, which is what makes that recoverable — the same
    uncommitted-work gap worktrees already have, rather than a new one.

    Every entry stays ``source="agent"``: a project's *agent* is still an agent, and filing
    it under "Project" would split one roster across two headings in the menu.
    """
    if LAUNCH_TOOL in withheld:
        return []
    declared = (
        project_agent_specs(root)
        if root is not None and is_project_folder(workspace_kind)
        else ()
    )
    roster = merged_roster(BUILTIN, declared)
    # Deduplicated on the *command* name, not the roster's: two entries whose names differ
    # only by `_` versus `-` collapse onto one typed handle, and shipping both would give
    # the loser a `shadowed_by` pointing at its own qualified name. First wins, which within
    # `merged_roster`'s ordering is the more local declaration.
    commands: dict[str, CommandSpec] = {}
    for spec in roster.values():
        command = _agent_command(spec)
        commands.setdefault(command.name, command)
    return list(commands.values())


def _agent_command(spec: SubagentSpec) -> CommandSpec:
    return CommandSpec(
        name=spec.name.replace("_", "-"),
        source="agent",
        kind="prompt",
        title=spec.name.replace("_", " "),
        # Taken verbatim. A roster line is written as a fragment that follows the
        # sub-agent's name ("explorer: searches and reads the workspace…"), which is
        # exactly how the picker stacks a title over a description — so rewriting it
        # here would mean two descriptions of one sub-agent, drifting apart.
        description=spec.description,
        target=spec.name,
        argument_hint="the brief for this sub-agent",
        argument_required=True,
    )


def _offered_in(spec: CommandSpec, workspace_kind: str) -> bool:
    return not spec.workspaces or workspace_kind in spec.workspaces


def _merge(specs: Sequence[CommandSpec]) -> tuple[CatalogEntry, ...]:
    """Rank the bare-name collisions, keeping every spec.

    Highest tier wins; a tie goes to the one declared first, which within a tier means the
    order the sources are assembled in above. Stable rather than clever: two sources at the
    same tier colliding on a name is already refused where each is created (a unique index,
    a first-wins file scan), so the tiebreak only has to be *decided*, not interesting.
    """
    winners: dict[str, CommandSpec] = {}
    for spec in specs:
        current = winners.get(spec.name)
        if current is None or spec.tier > current.tier:
            winners[spec.name] = spec
    return tuple(
        CatalogEntry(
            spec=spec,
            shadowed_by=(
                None
                if winners[spec.name] is spec
                else winners[spec.name].qualified_name
            ),
        )
        for spec in specs
    )
