"""Natural-language event entry (`CAL-3`) — "lunch Friday 1pm" becomes a draft.

The split here is the point: **the model reads the phrase, the code does the arithmetic.**
A utility-model call fills a small structured draft (title, a *local wall-clock* start,
optional end/location/rrule) and nothing else; converting that wall clock into a UTC
instant, defaulting a missing end, and validating the recurrence rule all happen
deterministically afterwards. Letting the model emit UTC — or a rule nobody parsed — is how
a calendar quietly acquires events an hour off, on the wrong day, or repeating forever.

The result is a **draft, not an event.** It is returned for the operator (or the agent's
own confirmation step) to accept; nothing is written here, so a misread phrase costs a
correction rather than a stray entry.

Like `services/webfetch/distill.py`, this builds a bare ``pydantic_ai.Agent`` over an
injected model resolver — a service must not import the engine layer.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings

from core.exceptions import DegradedCapabilityError
from models.calendar import DEFAULT_TIMEZONE, UTC_TIMEZONE
from prompts.calendar import CALENDAR_NL_INSTRUCTIONS
from services.calendar.recurrence import canonical_rrule, expand, parse_zone

logger = logging.getLogger(__name__)

# Yields the utility model plus its (reasoning-off) settings, fresh per call — the same
# seam the web distiller uses, so both share one wiring rule.
ResolveModel = Callable[[], Awaitable[tuple[Model, ModelSettings | None]]]

# A phrase is a phrase; anything longer is a paste, and feeding it whole only slows the
# call without sharpening the parse.
MAX_PHRASE_CHARS = 500
DEFAULT_TIMEOUT_S = 30.0
# What an event with no stated end becomes.
DEFAULT_DURATION = timedelta(hours=1)


class _Draft(BaseModel):
    """The model's output shape. Times are **local wall clock in the operator's zone**,
    deliberately without an offset — the model is not asked to do time-zone arithmetic,
    because that is the part it gets wrong and the part this module can do exactly."""

    title: str = Field(description="A short natural title for the event.")
    starts_at: datetime = Field(description="Local start, e.g. 2026-06-12T13:00:00.")
    ends_at: datetime | None = Field(default=None, description="Local end, if stated.")
    all_day: bool = Field(default=False, description="True only if no time is named.")
    location: str | None = Field(default=None, description="Only if a place is named.")
    description: str | None = Field(default=None, description="Only if extra detail is given.")
    rrule: str | None = Field(default=None, description="Bare RFC 5545 rule, if repeating.")


@dataclass(frozen=True)
class EventDraft:
    """A parsed phrase in the store's own vocabulary — UTC instants, a validated rule —
    ready to hand straight to ``create_event`` once the operator confirms it."""

    title: str
    starts_at: datetime
    ends_at: datetime
    timezone: str
    all_day: bool = False
    location: str | None = None
    description: str | None = None
    rrule: str | None = None


class CalendarNaturalLanguage:
    """Parses a phrase into an :class:`EventDraft` on the utility model."""

    def __init__(
        self,
        *,
        resolve_model: ResolveModel,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._resolve_model: ResolveModel = resolve_model
        self._timeout_s: float = timeout_s

    async def parse(
        self,
        phrase: str,
        *,
        timezone: str = DEFAULT_TIMEZONE,
        now: datetime | None = None,
    ) -> EventDraft:
        """Turn ``phrase`` into a draft.

        ``now`` and ``timezone`` are what relative references ("Friday", "tomorrow") are
        resolved against — supplied by the caller rather than read from the host clock's
        zone, so the same phrase parses identically wherever the backend runs.

        Raises :class:`ValueError` for an empty phrase and
        :class:`~core.exceptions.DegradedCapabilityError` when no utility model is
        available or the call fails — a `CAL-3` convenience degrades to "type it in
        yourself", it never guesses.
        """
        text = phrase.strip()
        if not text:
            raise ValueError("phrase must not be empty")
        zone = parse_zone(timezone)
        moment = (now or datetime.now(UTC)).astimezone(zone)

        try:
            model, settings = await self._resolve_model()
        except Exception as exc:  # noqa: BLE001 — an unbound/degraded role is not our error
            raise DegradedCapabilityError(
                "natural-language event entry needs a utility model to be configured"
            ) from exc

        agent = Agent(model, output_type=_Draft, instructions=CALENDAR_NL_INSTRUCTIONS)
        prompt = (
            f"CURRENT DATE AND TIME: {moment:%Y-%m-%dT%H:%M:%S} ({moment:%A})\n"
            f"OPERATOR TIME ZONE: {timezone}\n"
            f"PHRASE: {text[:MAX_PHRASE_CHARS]}"
        )
        try:
            result = await asyncio.wait_for(
                agent.run(prompt, model_settings=settings), self._timeout_s
            )
        except Exception as exc:  # noqa: BLE001 — timeout/model error ⇒ degrade, never guess
            logger.warning("calendar: natural-language parse failed: %s", exc)
            raise DegradedCapabilityError(f"could not parse {text!r} into an event") from exc

        return _to_draft(result.output, timezone=timezone, zone_hint=zone)


def _to_draft(draft: _Draft, *, timezone: str, zone_hint: ZoneInfo) -> EventDraft:
    """The deterministic half: localize the wall clock, default the span, validate the rule.

    A model that ignored the instruction and emitted an offset is respected rather than
    overridden — it already said which instant it meant.
    """
    all_day = bool(draft.all_day)
    zone = zone_hint
    start = _localize(draft.starts_at, zone)
    end = _localize(draft.ends_at, zone) if draft.ends_at is not None else None

    if all_day:
        # An all-day draft is a date, so it is pinned to UTC midnights exactly as the
        # store would — never localized, which is what keeps the day the same day.
        start = start.astimezone(zone).replace(hour=0, minute=0, second=0, microsecond=0)
        start = datetime(start.year, start.month, start.day, tzinfo=UTC)
        end = start + timedelta(days=1)
    elif end is None or end <= start:
        end = start + DEFAULT_DURATION

    return EventDraft(
        title=draft.title.strip() or "(untitled)",
        starts_at=start,
        ends_at=end,
        timezone=UTC_TIMEZONE if all_day else timezone,
        all_day=all_day,
        location=(draft.location or "").strip() or None,
        description=(draft.description or "").strip() or None,
        rrule=_safe_rrule(draft.rrule, start, end, timezone, all_day),
    )


def _localize(value: datetime, zone: ZoneInfo) -> datetime:
    """A naive wall clock is read in the operator's zone (what the prompt asked for); an
    aware one is taken at face value. Either way the result is UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=zone).astimezone(UTC)
    return value.astimezone(UTC)


def _safe_rrule(
    rule: str | None, start: datetime, end: datetime, timezone: str, all_day: bool
) -> str | None:
    """Keep a recurrence rule only if it actually parses. A hallucinated rule is dropped
    — a one-off event the operator repeats by hand is a smaller failure than a phantom
    series they have to hunt down."""
    if not rule or not rule.strip():
        return None
    canonical = canonical_rrule(rule)
    try:
        _ = expand(
            starts_at=start,
            ends_at=end,
            timezone=timezone,
            all_day=all_day,
            rrule=canonical,
            window_start=start,
            window_end=start,
        )
    except ValueError:
        logger.warning("calendar: dropping an unparseable generated rule %r", rule)
        return None
    return canonical
