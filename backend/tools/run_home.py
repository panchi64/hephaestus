"""The run's home for a caller that can do without one.

The guard, the read ledger and the file tools all place paths against the run's home. The
resolution is memoised on the run (``RunDeps.home``), so asking again costs nothing; this
is the one place that reads it, and it fails the same way for every caller: no home.
"""

from __future__ import annotations

import logging

from services.home.table import RunHome

from .deps import RunDeps
from .home import resolve_run_home

logger = logging.getLogger(__name__)


async def home_of(deps: RunDeps) -> RunHome | None:
    """This run's home, or None when it has none or cannot be opened."""
    if deps.home is not None:
        return deps.home
    try:
        return await resolve_run_home(deps)
    except Exception:  # a caller that raised here would abort the operator's turn
        logger.info("no home to place paths against", exc_info=True)
        return None
