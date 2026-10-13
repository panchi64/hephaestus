"""A routine's schedule: its shape, the checks a written one must pass, and the fire times
it will produce."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from croniter import croniter

from core.exceptions import RoutineValidationError
from models.task import ScheduleType
from services.calendar.recurrence import parse_zone
from services.scheduler import compute_next_run

_TYPES = frozenset(t.value for t in ScheduleType)


@dataclass(frozen=True)
class Schedule:
    """Exactly one of ``run_at``/``every_seconds``/``cron`` is meaningful, selected by
    ``type``; a ``webhook`` schedule uses none. ``timezone`` is what a cron's fields are
    read in."""

    type: str
    run_at: datetime | None = None
    every_seconds: float | None = None
    cron: str | None = None
    timezone: str = "UTC"

    def next_after(self, anchor: datetime) -> datetime | None:
        return compute_next_run(
            self.type,
            anchor=anchor,
            run_at=self.run_at,
            every_seconds=self.every_seconds,
            cron_expr=self.cron,
            timezone=self.timezone,
        )


def check_schedule(schedule: Schedule) -> None:
    def bad(message: str) -> RoutineValidationError:
        return RoutineValidationError("schedule", message)

    if schedule.type not in _TYPES:
        raise bad(f"unknown schedule type {schedule.type!r}")
    try:
        parse_zone(schedule.timezone)
    except ValueError:
        raise bad(f"unknown time zone {schedule.timezone!r}") from None
    if schedule.type == ScheduleType.ONCE:
        if schedule.run_at is None:
            raise bad("schedule.runAt is required for a 'once' schedule")
        if schedule.run_at.tzinfo is None:
            raise bad("schedule.runAt must carry a UTC offset")
    if schedule.type == ScheduleType.INTERVAL and (
        schedule.every_seconds is None or schedule.every_seconds <= 0
    ):
        raise bad("schedule.everySeconds must be a positive number for an 'interval' schedule")
    if schedule.type == ScheduleType.CRON:
        if not schedule.cron:
            raise bad("schedule.cron is required for a 'cron' schedule")
        try:
            croniter(schedule.cron)
        except (ValueError, KeyError) as exc:
            raise bad(f"invalid cron expression: {exc}") from None


def upcoming(schedule: Schedule, now: datetime, count: int = 3) -> list[datetime]:
    """The next ``count`` fire times after ``now``: what the schedule means, in instants."""
    if schedule.type == ScheduleType.ONCE:
        return [schedule.run_at] if schedule.run_at and schedule.run_at > now else []
    times: list[datetime] = []
    anchor = now
    while len(times) < count and (anchor := schedule.next_after(anchor)) is not None:
        times.append(anchor)
    return times


_UNITS = {"m": 60, "h": 3600, "d": 86400}


def parse_schedule(text: str, default_timezone: str) -> Schedule:
    """Read the compact form the agent writes: ``once <ISO time with offset>``,
    ``every <n><m|h|d>``, or ``cron <five fields> [zone]``. Raises
    :class:`RoutineValidationError` for anything else."""
    head, _, rest = text.strip().partition(" ")
    rest = rest.strip()
    if head == "once":
        try:
            run_at = datetime.fromisoformat(rest)
        except ValueError:
            raise RoutineValidationError("schedule", f"not an ISO time: {rest!r}") from None
        schedule = Schedule(type="once", run_at=run_at, timezone=default_timezone)
    elif head == "every" and rest[-1:] in _UNITS:
        try:
            amount = float(rest[:-1])
        except ValueError:
            raise RoutineValidationError("schedule", f"not a period: {rest!r}") from None
        seconds = amount * _UNITS[rest[-1]]
        schedule = Schedule(type="interval", every_seconds=seconds, timezone=default_timezone)
    elif head == "cron":
        fields = rest.split()
        zone = fields[5] if len(fields) == 6 else default_timezone
        schedule = Schedule(type="cron", cron=" ".join(fields[:5]), timezone=zone)
    else:
        raise RoutineValidationError(
            "schedule", "write 'once <ISO time>', 'every <n>m|h|d' or 'cron <fields> [zone]'"
        )
    check_schedule(schedule)
    return schedule


def format_schedule(schedule: Schedule) -> str:
    """The compact form :func:`parse_schedule` reads."""
    if schedule.type == ScheduleType.ONCE and schedule.run_at is not None:
        return f"once {schedule.run_at.isoformat()}"
    if schedule.type == ScheduleType.INTERVAL and schedule.every_seconds:
        return f"every {schedule.every_seconds / 60:g}m"
    if schedule.type == ScheduleType.CRON:
        return f"cron {schedule.cron} {schedule.timezone}"
    return schedule.type
