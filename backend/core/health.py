"""The status vocabulary every capability and notification channel reports in."""

from __future__ import annotations

from typing import Literal

#: ``off`` is something the operator hasn't set up or has switched off, which is not a
#: fault; ``warn`` and ``alert`` are faults.
HealthStatus = Literal["nominal", "warn", "alert", "off"]
