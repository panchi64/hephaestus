"""The operator's chat preferences: ``GET``/``PUT /chat/settings``."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import ValidationError

from routes import deps
from routes.chat.schemas import ChatSettings
from routes.deps import OPERATOR_ID
from runs import ContextThresholds
from services.settings_store import (
    AutoCompactSettings,
    get_agent_request_limit,
    get_auto_compact,
    get_context_thresholds,
    get_inactivity_timeout,
    get_subagent_limit,
    get_wall_clock_timeout,
    get_work_summary_idle_minutes,
    set_agent_request_limit,
    set_auto_compact,
    set_context_thresholds,
    set_inactivity_timeout,
    set_subagent_limit,
    set_wall_clock_timeout,
    set_work_summary_idle_minutes,
)

router = APIRouter(prefix="/chat", tags=["chat"])


def _settings_response(
    auto: AutoCompactSettings,
    context: ContextThresholds,
    steps: int,
    inactivity: float | None,
    wall_clock: float | None,
    subagents: int | None,
    work_summary_idle_minutes: int,
) -> ChatSettings:
    return ChatSettings(
        auto_compact_enabled=auto.enabled,
        auto_compact_threshold=auto.threshold,
        work_summary_idle_minutes=work_summary_idle_minutes,
        context_warn_threshold=context.warn,
        context_alert_threshold=context.alert,
        agent_request_limit=steps,
        inactivity_timeout_s=inactivity,
        wall_clock_timeout_s=wall_clock,
        subagent_max_concurrent=subagents,
    )


@router.get("/settings", response_model=ChatSettings)
async def get_chat_settings(request: Request) -> ChatSettings:
    """The operator's chat preferences (the runtime overrides, else the config defaults)."""
    store = deps.settings_store(request)
    auto = await get_auto_compact(store, OPERATOR_ID)
    steps = await get_agent_request_limit(store, OPERATOR_ID)
    inactivity = await get_inactivity_timeout(store, OPERATOR_ID)
    wall_clock = await get_wall_clock_timeout(store, OPERATOR_ID)
    context = await get_context_thresholds(store, OPERATOR_ID)
    subagents = await get_subagent_limit(store, OPERATOR_ID)
    idle_minutes = await get_work_summary_idle_minutes(store, OPERATOR_ID)
    return _settings_response(
        auto, context, steps, inactivity, wall_clock, subagents, idle_minutes
    )


@router.put("/settings", response_model=ChatSettings)
async def update_chat_settings(body: ChatSettings, request: Request) -> ChatSettings:
    """Persist the operator's chat preferences. The body's bounds reject a bad value before
    it reaches the store. Every field is optional and an omitted one is left unchanged —
    each multi-field group is merged over its current values and only written when the body
    touched it, so a client tuning one preference can't reset the rest."""
    store = deps.settings_store(request)
    if body.agent_request_limit is not None:
        steps = await set_agent_request_limit(store, OPERATOR_ID, body.agent_request_limit)
    else:
        steps = await get_agent_request_limit(store, OPERATOR_ID)
    if body.inactivity_timeout_s is not None:
        inactivity = await set_inactivity_timeout(store, OPERATOR_ID, body.inactivity_timeout_s)
    else:
        inactivity = await get_inactivity_timeout(store, OPERATOR_ID)
    # `null` here means "remove the bound", so presence in the body — not a non-None
    # value — is what says the client touched it.
    if "wall_clock_timeout_s" in body.model_fields_set:
        wall_clock = await set_wall_clock_timeout(store, OPERATOR_ID, body.wall_clock_timeout_s)
    else:
        wall_clock = await get_wall_clock_timeout(store, OPERATOR_ID)
    # `null` removes the cap here too, so presence rather than a non-None value is what
    # says the client touched it.
    if "subagent_max_concurrent" in body.model_fields_set:
        subagents = await set_subagent_limit(store, OPERATOR_ID, body.subagent_max_concurrent)
    else:
        subagents = await get_subagent_limit(store, OPERATOR_ID)

    if body.work_summary_idle_minutes is not None:
        idle_minutes = await set_work_summary_idle_minutes(
            store, OPERATOR_ID, body.work_summary_idle_minutes
        )
    else:
        idle_minutes = await get_work_summary_idle_minutes(store, OPERATOR_ID)

    auto = await get_auto_compact(store, OPERATOR_ID)
    if body.auto_compact_enabled is not None or body.auto_compact_threshold is not None:
        auto = await set_auto_compact(
            store,
            OPERATOR_ID,
            AutoCompactSettings(
                enabled=auto.enabled
                if body.auto_compact_enabled is None
                else body.auto_compact_enabled,
                threshold=auto.threshold
                if body.auto_compact_threshold is None
                else body.auto_compact_threshold,
            ),
        )

    context = await get_context_thresholds(store, OPERATOR_ID)
    if body.context_warn_threshold is not None or body.context_alert_threshold is not None:
        warn = context.warn if body.context_warn_threshold is None else body.context_warn_threshold
        alert = (
            context.alert if body.context_alert_threshold is None else body.context_alert_threshold
        )
        # The ordering invariant is checked on the *merged* pair, not on the body: raising
        # `warn` above a stored `alert` is the ordinary way to invert them, and a body
        # carrying only that one field would satisfy every per-field bound.
        # `ContextThresholds` owns the rule; this turns its refusal into the 422 the field
        # bounds would have produced had the constraint been expressible on one field.
        try:
            thresholds = ContextThresholds(warn=warn, alert=alert)
        except ValidationError as exc:
            raise HTTPException(
                status_code=422,
                detail=(
                    "the context warn threshold must be below the alert threshold "
                    f"(got warn {warn}, alert {alert})"
                ),
            ) from exc
        context = await set_context_thresholds(store, OPERATOR_ID, thresholds)
    return _settings_response(
        auto, context, steps, inactivity, wall_clock, subagents, idle_minutes
    )
