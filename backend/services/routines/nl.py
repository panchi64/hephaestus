"""Natural-language schedules — "every weekday at 9" becomes a :class:`Schedule`.

The calendar's split (`services/calendar/nl.py`): the model reads the phrase into local
wall-clock fields, the code localizes them and validates the result. Nothing is saved
here; the caller shows the schedule before anything uses it.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent

from core.exceptions import DegradedCapabilityError, InvalidInputError, RoutineValidationError
from prompts.routines import ROUTINE_SCHEDULE_INSTRUCTIONS
from services.calendar.nl import DEFAULT_TIMEOUT_S, MAX_PHRASE_CHARS, ResolveModel
from services.calendar.recurrence import parse_zone

from .schedule import Schedule, check_schedule

logger = logging.getLogger(__name__)


class _Draft(BaseModel):
    type: Literal["once", "interval", "cron"]
    run_at: datetime | None = Field(default=None, description="Local, e.g. 2026-06-12T09:00:00.")
    every_minutes: float | None = Field(default=None, description="The period, for 'interval'.")
    cron: str | None = Field(default=None, description="Five-field cron in local time.")


class ScheduleNaturalLanguage:
    """Parses a phrase into a :class:`Schedule` on the utility model."""

    def __init__(self, *, resolve_model: ResolveModel, timeout_s: float = DEFAULT_TIMEOUT_S):
        self._resolve_model = resolve_model
        self._timeout_s = timeout_s

    async def parse(self, phrase: str, *, timezone: str, now: datetime | None = None) -> Schedule:
        """Raises :class:`InvalidInputError` for an empty phrase or one that reads as no
        usable schedule, and :class:`DegradedCapabilityError` when no utility model is
        available or the call fails."""
        text = phrase.strip()
        if not text:
            raise InvalidInputError("phrase must not be empty")
        zone = parse_zone(timezone)
        moment = (now or datetime.now(UTC)).astimezone(zone)
        try:
            model, settings = await self._resolve_model()
        # An unbound or degraded role is not this parser's error.
        except Exception as exc:
            raise DegradedCapabilityError(
                "natural-language schedules need a utility model to be configured"
            ) from exc

        agent = Agent(model, output_type=_Draft, instructions=ROUTINE_SCHEDULE_INSTRUCTIONS)
        prompt = (
            f"CURRENT DATE AND TIME: {moment:%Y-%m-%dT%H:%M:%S} ({moment:%A})\n"
            f"OPERATOR TIME ZONE: {timezone}\n"
            f"PHRASE: {text[:MAX_PHRASE_CHARS]}"
        )
        try:
            result = await asyncio.wait_for(
                agent.run(prompt, model_settings=settings), self._timeout_s
            )
        # A timeout or model error degrades; it never guesses.
        except Exception as exc:
            logger.warning("routines: schedule parse failed: %s", exc)
            raise DegradedCapabilityError(f"could not read {text!r} as a schedule") from exc

        schedule = _to_schedule(result.output, timezone)
        try:
            check_schedule(schedule)
        except RoutineValidationError as exc:
            raise InvalidInputError(f"could not read {text!r} as a schedule: {exc}") from None
        if schedule.run_at is not None and schedule.run_at <= moment:
            raise InvalidInputError(f"{text!r} reads as a moment that has already passed")
        return schedule


def _to_schedule(draft: _Draft, timezone: str) -> Schedule:
    if draft.type == "once":
        run_at = draft.run_at
        if run_at is not None and run_at.tzinfo is None:
            run_at = run_at.replace(tzinfo=parse_zone(timezone))
        return Schedule(
            type="once",
            run_at=run_at.astimezone(UTC) if run_at else None,
            timezone=timezone,
        )
    if draft.type == "interval":
        seconds = timedelta(minutes=draft.every_minutes or 0).total_seconds()
        return Schedule(type="interval", every_seconds=seconds or None, timezone=timezone)
    return Schedule(type="cron", cron=(draft.cron or "").strip() or None, timezone=timezone)
