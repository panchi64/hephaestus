"""The model settings a harness run puts around its scenarios: the choosable list, the
``subagent`` role and per-agent pins, each named by ``<endpoint name>/<model>``."""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx
from pydantic import BaseModel, TypeAdapter

from evals.drive.instance import Binding, Endpoint, Instance, read

from .spec import Choice, Pin, Routing


class RefError(ValueError):
    """A ref names no endpoint the instance has."""


class _Choosable(BaseModel):
    endpoint_id: str
    model: str
    note: str = ""
    available: bool = True


class _Agent(BaseModel):
    endpoint_id: str | None = None
    model: str | None = None


class _Listed(_Agent):
    id: str


_CHOOSABLE: TypeAdapter[list[_Choosable]] = TypeAdapter(list[_Choosable])
_AGENTS: TypeAdapter[list[_Listed]] = TypeAdapter(list[_Listed])


def split_ref(ref: str) -> tuple[str, str]:
    """``(endpoint name, model)``; the endpoint name never holds a slash, a model id may."""
    name, slash, model = ref.partition("/")
    if not slash or not name or not model:
        raise RefError(f"{ref!r} is not <endpoint name>/<model>")
    return name, model


def fill(text: str, tokens: dict[str, str]) -> str:
    for token, value in tokens.items():
        text = text.replace(token, value)
    return text


@dataclass(slots=True)
class Router:
    """Applies routing to the instance and puts the run's own back after each scenario."""

    driver: Instance
    endpoints: dict[str, Endpoint]
    tokens: dict[str, str] = field(default_factory=dict)
    #: The run's own settings, which a scenario's routing temporarily replaces.
    choosable: list[Choice] = field(default_factory=list)
    subagent: str = ""
    _saved: bool = False
    _saved_choosable: list[_Choosable] = field(default_factory=list)
    _saved_pins: dict[str, _Agent] = field(default_factory=dict)
    #: The pins a scenario set, with what each agent had before it.
    _pinned: dict[str, _Agent] = field(default_factory=dict)

    def endpoint_id(self, ref: str) -> tuple[str, str]:
        name, model = split_ref(fill(ref, self.tokens))
        endpoint = self.endpoints.get(name)
        if endpoint is None:
            raise RefError(f"no endpoint named {name!r}; have {', '.join(sorted(self.endpoints))}")
        return endpoint.id, model

    async def save(self) -> None:
        """Keep the choosable list and the built-in agents' pins to restore at the end, and
        clear the pins: the scenarios assume only the pins they set themselves."""
        self._saved_choosable = read(await self.driver.http.get("/models/choosable"), _CHOOSABLE)
        self._saved = True
        agents = read(await self.driver.http.get("/agents"), _AGENTS)
        for agent in agents:
            if agent.id.startswith("builtin:") and (agent.endpoint_id or agent.model):
                self._saved_pins[agent.id] = _Agent(
                    endpoint_id=agent.endpoint_id, model=agent.model
                )
                await self._put_pin(agent.id, _Agent())

    async def restore(self) -> list[str]:
        """Put back what :meth:`save` kept, each piece on its own; returns what failed.

        A saved entry whose endpoint has since become unavailable can't be saved again as
        new, so it is dropped and named."""
        if not self._saved:
            return []
        problems: list[str] = []
        for agent_id, pin in self._saved_pins.items():
            try:
                await self._put_pin(agent_id, pin)
            except httpx.HTTPError as exc:
                problems.append(f"the pin on {agent_id}: {exc}")
        kept = self._saved_choosable
        failure = ""
        for entries in (kept, [c for c in kept if c.available]):
            response = await self.driver.http.put(
                "/models/choosable",
                json={"models": [c.model_dump(exclude={"available"}) for c in entries]},
            )
            if response.is_success:
                dropped = [f"{c.endpoint_id}/{c.model}" for c in kept if c not in entries]
                if dropped:
                    problems.append(f"unavailable choosable entries dropped: {dropped}")
                return problems
            failure = f"{response.status_code} {response.text}"
        problems.append(f"the choosable list: {failure}")
        return problems

    async def _put_pin(self, agent_id: str, pin: _Agent) -> None:
        response = await self.driver.http.put(f"/agents/{agent_id}/model", json=pin.model_dump())
        _ = response.raise_for_status()

    async def _set_choosable(self, entries: list[Choice]) -> None:
        models: list[dict[str, str]] = []
        for entry in entries:
            endpoint_id, model = self.endpoint_id(entry.ref)
            models.append({"endpoint_id": endpoint_id, "model": model, "note": entry.note})
        response = await self.driver.http.put("/models/choosable", json={"models": models})
        _ = response.raise_for_status()

    async def _set_subagent(self, ref: str) -> None:
        if not ref:
            await self.driver.bind("subagent", Binding(endpoint_ids=[]))
            return
        endpoint_id, model = self.endpoint_id(ref)
        await self.driver.bind("subagent", Binding(endpoint_ids=[endpoint_id], model=model))

    async def _pin(self, pin: Pin) -> None:
        if pin.agent not in self._pinned:
            found = await self.driver.http.get(f"/agents/{pin.agent}")
            self._pinned[pin.agent] = read(found, _Agent)
        endpoint_id, model = self.endpoint_id(pin.ref)
        await self._put_pin(pin.agent, _Agent(endpoint_id=endpoint_id, model=model))

    async def run_defaults(self) -> None:
        await self._set_choosable(self.choosable)
        await self._set_subagent(self.subagent)

    async def apply(self, routing: Routing) -> None:
        if routing.choosable is not None:
            await self._set_choosable(routing.choosable)
        if routing.subagent is not None:
            await self._set_subagent(routing.subagent)
        for pin in routing.pins:
            await self._pin(pin)

    async def reset(self) -> None:
        """Back to the run's own settings, pins as they were before the scenario."""
        await self.run_defaults()
        for agent, was in self._pinned.items():
            await self._put_pin(agent, was)
        self._pinned.clear()

    def resolvable(self, routing: Routing | None) -> str | None:
        """Why ``routing`` can't run here, or None when every ref it names resolves."""
        for ref in routing.refs() if routing else []:
            try:
                _ = self.endpoint_id(ref)
            except RefError as exc:
                return str(exc)
        return None
