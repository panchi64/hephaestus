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
``agents_spawn`` has been withheld (the operator's own switch, offline mode —
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

from services.agents import (
    BUILTIN,
    AgentCatalog,
    AgentDefinition,
    delegable,
    merged_roster,
    project_agents,
)
from services.skills import SkillStore
from services.workspace import PlaceKind

from .actions import BUILTIN_ACTIONS, NEEDS_CONVERSATION
from .definitions import project_specs as project_command_specs
from .spec import CommandSpec
from .store import CommandStore

#: The tool a sub-agent command exists to ask for. Withheld — by the operator's switch, by
#: offline mode — and the command is an offer the turn cannot honour.
LAUNCH_TOOL = "agents_spawn"

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

    def __init__(
        self, skills: SkillStore, commands: CommandStore, agents: AgentCatalog | None = None
    ) -> None:
        self._skills: SkillStore = skills
        self._commands: CommandStore = commands
        self._agents: AgentCatalog | None = agents

    async def catalog(
        self,
        owner_id: str,
        *,
        place: PlaceKind,
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
        # operator wrote, then what the project declared — so that `merge`'s tiebreak
        # between equals is also source order, and reading this list top to bottom is
        # reading the precedence rule. Actions and agents are both tier 0 and must stay
        # above skills and workflows, both tier 1, for that to remain true.
        specs: list[CommandSpec] = [
            *_actions(has_conversation=has_conversation),
            *_agent_specs(await self._roster(owner_id, root, place), withheld=withheld),
            *await self._skill_specs(owner_id),
            *await self._commands.specs(owner_id),
            *(project_command_specs(root) if root is not None else ()),
        ]
        return merge([spec for spec in specs if _offered_in(spec, place)])

    async def resolve(
        self,
        owner_id: str,
        name: str,
        *,
        place: PlaceKind,
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
            place=place,
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

    async def _roster(
        self, owner_id: str, root: Path | None, place: PlaceKind
    ) -> dict[str, AgentDefinition]:
        """The agents a launch from this thread could reach — the same catalog
        ``tools/project_agents.run_roster`` resolves, so the picker never offers a name the
        launch would not find. A project's files count only in a thread filed under it."""
        project_root = root if root is not None and place != "sandbox" else None
        if self._agents is None:
            declared = project_agents(project_root) if project_root is not None else ()
            return delegable(merged_roster(BUILTIN, declared))
        return delegable(await self._agents.agents(owner_id, project_root=project_root))

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
        spec for spec in BUILTIN_ACTIONS if has_conversation or spec.name not in NEEDS_CONVERSATION
    ]


def _agent_specs(
    roster: dict[str, AgentDefinition], *, withheld: frozenset[str]
) -> list[CommandSpec]:
    """One command per agent the thread could delegate to.

    Deduplicated on the *command* name: two agents whose names differ only by ``_`` versus
    ``-`` collapse onto one typed handle, and the first (the more local) wins. Every entry
    stays ``source="agent"``, wherever the agent was declared.

    In a worktree thread before its first turn, ``root`` is still the operator's own
    checkout, so an agent file they have not committed is offered and will not be in the
    worktree the launcher resolves against; the command carries only the name, which keeps
    that recoverable.
    """
    if LAUNCH_TOOL in withheld:
        return []
    commands: dict[str, CommandSpec] = {}
    for agent in roster.values():
        command = _agent_command(agent)
        _ = commands.setdefault(command.name, command)
    return list(commands.values())


def _agent_command(agent: AgentDefinition) -> CommandSpec:
    return CommandSpec(
        name=agent.name.replace("_", "-"),
        source="agent",
        kind="prompt",
        title=agent.name.replace("_", " "),
        # Taken verbatim. A roster line is written as a fragment that follows the
        # sub-agent's name ("explorer: searches and reads the workspace…"), which is
        # exactly how the picker stacks a title over a description — so rewriting it
        # here would mean two descriptions of one sub-agent, drifting apart.
        description=agent.description,
        target=agent.name,
        argument_hint="the brief for this sub-agent",
        argument_required=True,
    )


def _offered_in(spec: CommandSpec, place: PlaceKind) -> bool:
    return not spec.workspaces or place in spec.workspaces


def merge(specs: Sequence[CommandSpec]) -> tuple[CatalogEntry, ...]:
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
            shadowed_by=(None if winners[spec.name] is spec else winners[spec.name].qualified_name),
        )
        for spec in specs
    )
