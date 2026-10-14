"""A typed service container — capability handles keyed by concrete class.

The one shape used everywhere a capability is looked up rather than imported:
feature manifests resolve their cross-feature dependencies from the harness's
container, and the agent's tools reach their capabilities through the run's bag
(``RunDeps.caps``). Lives in ``core`` so every layer above can share it without
a cycle.
"""

from __future__ import annotations

from typing import cast


class ServiceContainer:
    """Instances keyed by their concrete type.

    ``get`` is loud — missing means a wiring bug (a manifest forgot an ``after``
    edge or an export). Optionality is the *caller's* semantic and spelled
    ``get_optional``: the agent's tools use it and degrade to an "unavailable"
    result when a capability isn't wired, never crash the turn.
    """

    def __init__(self) -> None:
        self._services: dict[type, object] = {}

    @classmethod
    def of(cls, *instances: object) -> ServiceContainer:
        """A bag from a handful of handles, each keyed by its concrete type."""
        container = cls()
        for instance in instances:
            container.add(instance)
        return container

    def add(self, instance: object, *, as_type: type | None = None) -> None:
        key = as_type if as_type is not None else type(instance)
        if key in self._services:
            raise LookupError(f"service already registered for {key.__name__}")
        self._services[key] = instance

    def get[T](self, service_type: type[T]) -> T:
        try:
            return cast(T, self._services[service_type])
        except KeyError:
            raise LookupError(
                f"no service registered for {service_type.__name__} — "
                + "a manifest is missing an `after` edge or an export"
            ) from None

    def get_optional[T](self, service_type: type[T]) -> T | None:
        return cast(T | None, self._services.get(service_type))
