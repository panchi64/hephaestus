"""Natural-language event entry (`CAL-3`).

The model is a `FunctionModel` stand-in throughout: what's under test is the deterministic
half — localizing the wall clock the model emits, defaulting a missing end, validating the
rule, and degrading rather than guessing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from pydantic_ai import ModelMessage, ModelResponse, ToolCallPart, UserPromptPart
from pydantic_ai.models import Model
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.settings import ModelSettings

from core.exceptions import DegradedCapabilityError
from services.calendar.nl import CalendarNaturalLanguage

MADRID = ZoneInfo("Europe/Madrid")
NOW = datetime(2026, 6, 10, 9, 0, tzinfo=UTC)  # a Wednesday


def _model(**fields: str | bool) -> tuple[FunctionModel, dict[str, str]]:
    """A model that always answers with ``fields`` as its structured output, and records
    the prompt it was given."""
    seen: dict[str, str] = {}

    async def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        part = messages[-1].parts[-1]
        assert isinstance(part, UserPromptPart)
        seen["prompt"] = str(part.content)
        tool = info.output_tools[0].name
        return ModelResponse(parts=[ToolCallPart(tool_name=tool, args=dict(fields))])

    return FunctionModel(respond), seen


def _parser(model: FunctionModel) -> CalendarNaturalLanguage:
    async def resolve() -> tuple[Model, ModelSettings | None]:
        return model, None

    return CalendarNaturalLanguage(resolve_model=resolve)


async def test_a_local_wall_clock_becomes_the_right_utc_instant():
    """13:00 in Madrid in June (UTC+2) is 11:00 UTC — the arithmetic the model is
    deliberately not asked to do."""
    model, _ = _model(title="Lunch with Ana", starts_at="2026-06-12T13:00:00")
    draft = await _parser(model).parse(
        "lunch with Ana Friday 1pm", timezone="Europe/Madrid", now=NOW
    )

    assert draft.title == "Lunch with Ana"
    assert draft.starts_at == datetime(2026, 6, 12, 11, 0, tzinfo=UTC)
    assert draft.starts_at.astimezone(MADRID).hour == 13
    assert draft.timezone == "Europe/Madrid"


async def test_a_missing_end_becomes_an_hour():
    model, _ = _model(title="Coffee", starts_at="2026-06-12T13:00:00")
    draft = await _parser(model).parse("coffee Friday 1pm", timezone="UTC", now=NOW)
    assert draft.ends_at == datetime(2026, 6, 12, 14, tzinfo=UTC)


async def test_a_stated_end_is_kept():
    model, _ = _model(
        title="Workshop", starts_at="2026-06-12T09:00:00", ends_at="2026-06-12T17:00:00"
    )
    draft = await _parser(model).parse("workshop Friday 9 to 5", timezone="UTC", now=NOW)
    assert draft.ends_at == datetime(2026, 6, 12, 17, tzinfo=UTC)


async def test_an_inverted_end_is_repaired_rather_than_stored():
    model, _ = _model(
        title="Muddle", starts_at="2026-06-12T13:00:00", ends_at="2026-06-12T09:00:00"
    )
    draft = await _parser(model).parse("something confusing", timezone="UTC", now=NOW)
    assert draft.ends_at == datetime(2026, 6, 12, 14, tzinfo=UTC)


async def test_an_all_day_draft_is_pinned_to_utc_midnights_on_the_local_date():
    """Even parsed in a zone two hours ahead, "Friday" is Friday — not Thursday 22:00."""
    model, _ = _model(title="Public holiday", starts_at="2026-06-12T00:00:00", all_day=True)
    draft = await _parser(model).parse("holiday Friday", timezone="Europe/Madrid", now=NOW)

    assert draft.all_day is True
    assert draft.starts_at == datetime(2026, 6, 12, tzinfo=UTC)
    assert draft.ends_at == datetime(2026, 6, 13, tzinfo=UTC)
    assert draft.timezone == "UTC"


async def test_an_offset_the_model_supplied_is_respected():
    """A model that ignored the "no offset" instruction already said which instant it
    meant; overriding that would move the event."""
    model, _ = _model(title="Call", starts_at="2026-06-12T13:00:00+00:00")
    draft = await _parser(model).parse("call Friday", timezone="Europe/Madrid", now=NOW)
    assert draft.starts_at == datetime(2026, 6, 12, 13, tzinfo=UTC)


async def test_a_recurrence_the_phrase_implies_is_kept_and_canonicalized():
    model, _ = _model(
        title="Standup", starts_at="2026-06-12T09:00:00", rrule="RRULE:FREQ=WEEKLY;BYDAY=FR"
    )
    draft = await _parser(model).parse("standup every Friday 9am", timezone="UTC", now=NOW)
    assert draft.rrule == "FREQ=WEEKLY;BYDAY=FR"


async def test_a_hallucinated_rule_is_dropped_not_stored():
    model, _ = _model(title="Standup", starts_at="2026-06-12T09:00:00", rrule="FREQ=SOMETIMES")
    draft = await _parser(model).parse("standup sometimes", timezone="UTC", now=NOW)
    assert draft.rrule is None


async def test_blank_optional_fields_come_back_as_none():
    model, _ = _model(
        title=" Lunch ", starts_at="2026-06-12T13:00:00", location="   ", description=""
    )
    draft = await _parser(model).parse("lunch Friday", timezone="UTC", now=NOW)
    assert draft.title == "Lunch"
    assert draft.location is None and draft.description is None


async def test_the_prompt_carries_now_and_the_zone_so_relative_dates_resolve():
    model, seen = _model(title="Lunch", starts_at="2026-06-12T13:00:00")
    _ = await _parser(model).parse("lunch Friday 1pm", timezone="Europe/Madrid", now=NOW)

    assert "2026-06-10T11:00:00" in seen["prompt"]  # 09:00 UTC rendered in Madrid
    assert "Wednesday" in seen["prompt"]
    assert "Europe/Madrid" in seen["prompt"]
    assert "lunch Friday 1pm" in seen["prompt"]


async def test_an_empty_phrase_is_rejected():
    model, _ = _model(title="x", starts_at="2026-06-12T13:00:00")
    with pytest.raises(ValueError):
        _ = await _parser(model).parse("   ", timezone="UTC", now=NOW)


async def test_an_unknown_zone_is_rejected():
    model, _ = _model(title="x", starts_at="2026-06-12T13:00:00")
    with pytest.raises(ValueError):
        _ = await _parser(model).parse("lunch", timezone="Mars/Olympus_Mons", now=NOW)


async def test_no_utility_model_degrades_rather_than_guessing():
    async def resolve() -> tuple[Model, ModelSettings | None]:
        raise RuntimeError("no utility role bound")

    parser = CalendarNaturalLanguage(resolve_model=resolve)
    with pytest.raises(DegradedCapabilityError):
        _ = await parser.parse("lunch Friday 1pm", timezone="UTC", now=NOW)


async def test_a_model_error_degrades_rather_than_guessing():
    async def broken(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        raise RuntimeError("the model fell over")

    parser = _parser(FunctionModel(broken))
    with pytest.raises(DegradedCapabilityError):
        _ = await parser.parse("lunch Friday 1pm", timezone="UTC", now=NOW)
