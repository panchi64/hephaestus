"""What a recorded sample left behind for its checks to read."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict

from evals.drive.checks import Root
from evals.drive.timeline import SubagentRecord, Tool, Turn

type Where = Literal["parent", "subagent", "any"]


class ProxiedRequest(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    endpoint: str
    model: str
    tools: bool


@dataclass(frozen=True, slots=True)
class Evidence:
    """What one sample left behind."""

    #: The first thread's turns in order, sub-agent deliveries included.
    turns: list[Turn]
    #: The ``new_thread`` prompts' turns.
    later: list[Turn] = field(default_factory=list)
    subagents: list[SubagentRecord] = field(default_factory=list)
    #: The first thread's task list at the end, as ``(status, content)``.
    tasks: list[tuple[str, str]] = field(default_factory=list)
    requests: list[ProxiedRequest] = field(default_factory=list)
    title: str | None = None
    sandbox: Path | None = None
    project: Path | None = None
    #: The fixture the project was copied from, for ``file_unchanged``.
    fixture: Path | None = None
    #: Indices into :attr:`turns` of the turns a scenario action cancelled.
    cancelled: frozenset[int] = frozenset()
    #: ``{local}`` and ``{hosted}`` as ``<endpoint name>/<model>``.
    refs: Mapping[str, str] = field(default_factory=dict)

    @property
    def answer(self) -> str:
        """The last reply the operator read, in whichever thread came last."""
        for turn in reversed([*self.turns, *self.later]):
            if reply := turn.final_reply():
                return reply
        return ""

    def reply(self, turn: int | None) -> str:
        """The final reply of operator turn ``turn``, from 1; :attr:`answer` for None."""
        if turn is None:
            return self.answer
        turns = self.operator_turns()
        return turns[turn - 1].final_reply() if 0 < turn <= len(turns) else ""

    def file(self, root: Root, path: str) -> Path | None:
        base = self.sandbox if root == "sandbox" else self.project
        return base / path if base is not None else None

    def operator_turns(self) -> list[Turn]:
        return [t for t in self.turns if not t.delivery]

    def parent_turns(self) -> list[Turn]:
        return [*self.turns, *self.later]

    def child_turns(self, agent_name: str | None = None) -> list[Turn]:
        return [
            turn
            for agent in self.subagents
            if agent_name is None or agent.name == agent_name
            for turn in agent.turns
        ]

    def tools(self, where: Where) -> Iterator[Tool]:
        turns = {
            "parent": self.parent_turns(),
            "subagent": self.child_turns(),
            "any": [*self.parent_turns(), *self.child_turns()],
        }[where]
        for turn in turns:
            yield from turn.tools()

    def resolve(self, ref: str) -> str:
        return self.refs.get(ref, ref)
