"""Third-party connectors (`INTEG-1`…`INTEG-3`).

Two pieces: :mod:`services.integrations.presets` is the static catalog — what the system
knows about a service before the operator supplies anything (address, auth shape, a
credential-proving request, the actions worth exposing) — and
:mod:`services.integrations.service` owns the operator's side: configure from a preset with
the credential sealed, test it, call an action, and the per-action enable/trust decisions
that ride the shared :mod:`services.external_policy` store.
"""

from .presets import PRESETS, IntegrationAction, IntegrationPreset, action, preset
from .service import (
    MAX_BODY_CHARS,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_UNTESTED,
    IntegrationActionView,
    IntegrationResponse,
    IntegrationService,
    IntegrationView,
)

__all__ = [
    "MAX_BODY_CHARS",
    "PRESETS",
    "STATUS_ERROR",
    "STATUS_OK",
    "STATUS_UNTESTED",
    "IntegrationAction",
    "IntegrationActionView",
    "IntegrationPreset",
    "IntegrationResponse",
    "IntegrationService",
    "IntegrationView",
    "action",
    "preset",
]
