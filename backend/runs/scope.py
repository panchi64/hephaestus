"""The run the current task is working for, so work it spawns can be attributed to it."""

from __future__ import annotations

from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from runs.run import Run

#: Set for the body of `RunRegistry._execute`; tasks created inside inherit it, which is
#: how an auto-title or a page distill started by a turn is traced to that turn.
current_run: ContextVar[Run | None] = ContextVar("current_run", default=None)
