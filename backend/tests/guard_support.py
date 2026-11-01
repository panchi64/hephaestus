"""Shared by the guard tests: pin where the host fence stands."""

from __future__ import annotations

from typing import Literal

import pytest

from core.config import Settings
from services.permissions import guards
from services.sandbox.fence import ConfinementState

type FenceState = Literal["active", "disabled_by_setting", "unavailable"]


def set_fence(monkeypatch: pytest.MonkeyPatch, state: FenceState, reason: str = "") -> None:
    def fixed(_settings: Settings) -> ConfinementState:
        return ConfinementState(state, reason)

    monkeypatch.setattr(guards, "confinement_state", fixed)
