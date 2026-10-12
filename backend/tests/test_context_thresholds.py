"""Where the context gauge changes colour, and who decides.

The boundaries used to be two literals inside the derivation. They are now the
operator's, because the fullness at which a window's remaining room stops being enough
depends on how someone works rather than on the model — a thread of long tool results
can spend its last quarter in one turn. What that change has to preserve is that there
is still exactly *one* boundary in play: the level travels on the wire, and a live turn,
a reloaded thread and an unattended task must all resolve it against the same pair.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.db import init_db, make_engine
from runs import DEFAULT_CONTEXT_THRESHOLDS, ContextThresholds, ContextWindow
from runs.events import RunMetrics
from services.settings_store import (
    CONTEXT_ALERT_THRESHOLD_KEY,
    CONTEXT_WARN_THRESHOLD_KEY,
    SettingsStore,
    get_context_thresholds,
    set_context_thresholds,
)

from ._helpers import client_app


def _store() -> SettingsStore:
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    return SettingsStore(engine)


# ── The invariant ────────────────────────────────────────────────────────────────


def test_warn_must_sit_below_alert():
    # Equal boundaries make the amber band unreachable and inverted ones walk the gauge
    # backwards through severity as it fills; neither is a preference worth honouring.
    with pytest.raises(ValidationError):
        ContextThresholds(warn=0.9, alert=0.9)
    with pytest.raises(ValidationError):
        ContextThresholds(warn=0.95, alert=0.5)


@pytest.mark.parametrize(("warn", "alert"), [(0, 0.9), (0.75, 0), (0.75, 1.5), (1.0, 1.0)])
def test_a_boundary_outside_the_window_is_refused(warn: float, alert: float):
    # A fraction is 0-1 by construction: outside it the boundary names a fullness the
    # gauge can never reach, so the band it opens can never be entered.
    with pytest.raises(ValidationError):
        ContextThresholds(warn=warn, alert=alert)


def test_alert_may_sit_at_a_full_window():
    # The one boundary that legitimately touches its bound: an operator who only wants
    # to hear about a window that is actually full sets alert to 1.
    assert ContextThresholds(warn=0.5, alert=1.0).alert == 1.0


# ── Derivation against the operator's pair ───────────────────────────────────────


@pytest.mark.parametrize(
    ("used", "expected"),
    [
        (100_000, "nominal"),
        (119_999, "nominal"),
        (120_000, "warn"),
        (159_999, "warn"),
        (160_000, "alert"),
    ],
)
def test_the_operators_boundaries_move_the_bands(used: int, expected: str):
    # 60% (120k) and 80% (160k) against a 200k window, in place of the stock 75/90.
    state = ContextWindow.from_used(used, 200_000, ContextThresholds(warn=0.6, alert=0.8))
    assert state is not None
    assert state.level == expected


def test_the_default_pair_is_what_an_uninformed_caller_gets():
    # `from_used` is called from places with no settings store to consult (a stateless
    # turn, a test). Those keep the stock boundaries rather than losing severity entirely.
    assert ContextWindow.from_used(160_000, 200_000) == ContextWindow.from_used(
        160_000, 200_000, DEFAULT_CONTEXT_THRESHOLDS
    )


def test_the_metrics_frame_applies_the_pair_it_carries():
    metrics = RunMetrics(
        context_used=130_000,
        context_window=200_000,
        context_thresholds=ContextThresholds(warn=0.6, alert=0.8),
    )
    assert metrics.context is not None
    assert metrics.context.level == "warn"  # 65% — nominal under the stock 75/90


def test_the_pair_stays_off_the_wire():
    """It is an *input* to the derivation, not part of the readout. Serializing it would
    invite a client to re-derive the level it is already being handed — the one thing the
    'clients render it, they never derive it' rule exists to prevent."""
    wire = RunMetrics(context_used=95, context_window=100).model_dump()
    assert "context_thresholds" not in wire
    assert wire["context"]["level"] == "alert"


# ── Storage: a corrupted pair falls back whole ───────────────────────────────────


async def test_a_stored_pair_round_trips():
    store = _store()
    await set_context_thresholds(store, "operator", ContextThresholds(warn=0.5, alert=0.8))
    assert await get_context_thresholds(store, "operator") == ContextThresholds(warn=0.5, alert=0.8)


async def test_unset_yields_the_defaults():
    assert await get_context_thresholds(_store(), "operator") == DEFAULT_CONTEXT_THRESHOLDS


@pytest.mark.parametrize(
    ("warn", "alert"),
    [
        ("not-a-number", "0.9"),  # unparseable
        ("0.95", "0.9"),  # inverted as stored
        ("2", "0.9"),  # out of range
    ],
)
async def test_a_bad_stored_pair_falls_back_whole(warn: str, alert: str):
    store = _store()
    await store.set("operator", CONTEXT_WARN_THRESHOLD_KEY, warn)
    await store.set("operator", CONTEXT_ALERT_THRESHOLD_KEY, alert)
    assert await get_context_thresholds(store, "operator") == DEFAULT_CONTEXT_THRESHOLDS


async def test_a_half_written_pair_falls_back_whole():
    """All-or-nothing, not per-field. A stored `warn` of 0.95 read beside a *defaulted*
    `alert` of 0.9 is an inverted pair no operator ever chose — and the operator has no
    way to tell a miscalibrated gauge from a correct one by looking at it."""
    store = _store()
    await store.set("operator", CONTEXT_WARN_THRESHOLD_KEY, "0.95")
    assert await get_context_thresholds(store, "operator") == DEFAULT_CONTEXT_THRESHOLDS


# ── The settings surface ─────────────────────────────────────────────────────────


async def test_get_reports_the_effective_pair():
    async with client_app() as (client, _):
        body = (await client.get("/chat/settings")).json()
        assert body["context_warn_threshold"] == DEFAULT_CONTEXT_THRESHOLDS.warn
        assert body["context_alert_threshold"] == DEFAULT_CONTEXT_THRESHOLDS.alert


async def test_put_persists_and_echoes():
    async with client_app() as (client, _):
        resp = await client.put(
            "/chat/settings",
            json={"context_warn_threshold": 0.6, "context_alert_threshold": 0.85},
        )
        assert resp.status_code == 200
        assert resp.json()["context_warn_threshold"] == 0.6
        assert (await client.get("/chat/settings")).json()["context_alert_threshold"] == 0.85


async def test_one_field_moves_alone():
    async with client_app() as (client, _):
        await client.put("/chat/settings", json={"context_warn_threshold": 0.5})
        body = (await client.get("/chat/settings")).json()
        assert body["context_warn_threshold"] == 0.5
        assert body["context_alert_threshold"] == DEFAULT_CONTEXT_THRESHOLDS.alert


async def test_a_single_field_that_inverts_the_pair_is_refused():
    """The reason the ordering check is on the merged pair rather than on the body: this
    request satisfies every per-field bound and still asks for an impossible gauge."""
    async with client_app() as (client, _):
        resp = await client.put("/chat/settings", json={"context_warn_threshold": 0.95})
        assert resp.status_code == 422
        assert "below the alert threshold" in str(resp.json())
        # And nothing was written — a refused PUT leaves the gauge as it was.
        body = (await client.get("/chat/settings")).json()
        assert body["context_warn_threshold"] == DEFAULT_CONTEXT_THRESHOLDS.warn


async def test_tuning_the_gauge_leaves_the_other_preferences_alone():
    async with client_app() as (client, _):
        before = (await client.get("/chat/settings")).json()
        await client.put("/chat/settings", json={"context_warn_threshold": 0.55})
        after = (await client.get("/chat/settings")).json()
        for key in ("auto_compact_enabled", "auto_compact_threshold", "agent_request_limit"):
            assert after[key] == before[key]


# ── The whole chain ──────────────────────────────────────────────────────────────


async def _level_under(thresholds: ContextThresholds | None) -> tuple[str, float]:
    """Run one real turn and report the level (and fullness) its metrics carry."""
    from pydantic_ai.models.test import TestModel

    from agent import build_chat_orchestrator
    from runs import RunRegistry, RunStatus

    kwargs = {} if thresholds is None else {"context_thresholds": thresholds}
    # `call_tools=[]` keeps it a plain text turn: the default catalog carries an
    # approval-gated tool that TestModel would otherwise call and park the run on.
    orch = build_chat_orchestrator(
        "hi",
        model=TestModel(custom_output_text="ok", call_tools=[]),
        context_window=100_000,
        **kwargs,
    )
    run = RunRegistry().submit(kind="chat", owner_id="operator", orchestrator=orch)
    await run.wait()
    assert run.status is RunStatus.done
    frame = [e.body for e in run.stream.replay() if e.body.type == "run.metrics"][-1]
    assert frame.context is not None
    return frame.context.level, frame.context.fraction


async def test_the_operators_pair_reaches_a_live_turns_metrics():
    """The link every unit test above stops short of: settings → `compose_turn` →
    `run.context_thresholds` → the emitted frame. Four hops, none of which fail loudly if
    one is dropped — the gauge would simply go on reporting the stock boundaries, and the
    operator's setting would look like it did nothing."""
    stock_level, fraction = await _level_under(None)
    assert stock_level == "nominal"  # a one-turn thread is nowhere near 75% of 100k

    # Boundaries placed either side of that same thread's measured fullness: nothing about
    # the turn changed, only where the operator drew the line.
    moved, _ = await _level_under(ContextThresholds(warn=fraction / 2, alert=0.9))
    assert moved == "warn"
    moved, _ = await _level_under(ContextThresholds(warn=fraction / 4, alert=fraction / 2))
    assert moved == "alert"
