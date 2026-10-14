"""The provider registry — pkgutil-discovered adapters, presence is registration.

Every module in this package exposing ``PROVIDER`` is an adapter (the same
contract as ``harness/manifests`` and the backup marker); ``get_provider`` is how
the registry, the builders, and the routes dispatch on ``ModelEndpoint.provider``.
An unknown id is a wiring/validation bug and raises loudly.
"""

from __future__ import annotations

import importlib
import pkgutil

from services.providers.base import Provider, ProviderPreset

__all__ = ["Provider", "ProviderPreset", "all_providers", "get_provider"]

DEFAULT_PROVIDER_ID = "openai-compatible"


def _discover() -> dict[str, Provider]:
    import services.providers as pkg

    providers: dict[str, Provider] = {}
    for module_info in pkgutil.iter_modules(pkg.__path__):
        if module_info.name.startswith("_") or module_info.name == "base":
            continue
        module = importlib.import_module(f"services.providers.{module_info.name}")
        provider = getattr(module, "PROVIDER", None)
        if provider is None:
            continue
        if provider.id in providers:
            raise RuntimeError(f"provider id {provider.id!r} declared twice")
        providers[provider.id] = provider
    return providers


_PROVIDERS: dict[str, Provider] = {}


def _registry() -> dict[str, Provider]:
    # Deferred (not import-time) so the package can be imported without pulling in
    # every SDK; built once per process thereafter.
    if not _PROVIDERS:
        _PROVIDERS.update(_discover())
    return _PROVIDERS


def get_provider(provider_id: str) -> Provider:
    try:
        return _registry()[provider_id]
    except KeyError:
        raise LookupError(f"unknown model provider {provider_id!r}") from None


def all_providers() -> list[Provider]:
    """Every adapter, stable-ordered by id — the ``GET /models/providers`` listing."""
    return [p for _, p in sorted(_registry().items())]
