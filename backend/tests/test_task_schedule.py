"""Pure next-run-time math (`compute_next_run`) — deterministic, no DB, no clock.
Shared by the scheduler's tick loop and (later) the task-creation route."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from plugins.routines.models import ScheduleType
from plugins.routines.scheduler import compute_next_run


def test_once_returns_its_run_at_unchanged_regardless_of_anchor():
    run_at = datetime(2026, 1, 1, tzinfo=UTC)
    anchor = datetime(2026, 6, 1, tzinfo=UTC)  # far from run_at — must not matter
    assert compute_next_run(ScheduleType.ONCE, anchor=anchor, run_at=run_at) == run_at


def test_interval_anchors_forward_from_the_given_anchor():
    anchor = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    next_run = compute_next_run(ScheduleType.INTERVAL, anchor=anchor, every_seconds=60)
    assert next_run == anchor + timedelta(seconds=60)


def test_interval_accepts_sub_second_intervals():
    anchor = datetime(2026, 1, 1, tzinfo=UTC)
    next_run = compute_next_run(ScheduleType.INTERVAL, anchor=anchor, every_seconds=0.25)
    assert next_run == anchor + timedelta(seconds=0.25)


def test_interval_missing_or_non_positive_every_seconds_degrades_to_never_due():
    anchor = datetime(2026, 1, 1, tzinfo=UTC)
    assert compute_next_run(ScheduleType.INTERVAL, anchor=anchor, every_seconds=None) is None
    assert compute_next_run(ScheduleType.INTERVAL, anchor=anchor, every_seconds=0) is None
    assert compute_next_run(ScheduleType.INTERVAL, anchor=anchor, every_seconds=-5) is None


def test_cron_computes_the_next_matching_slot_after_the_anchor():
    anchor = datetime(2026, 1, 1, 12, 3, 0, tzinfo=UTC)
    next_run = compute_next_run(ScheduleType.CRON, anchor=anchor, cron_expr="*/5 * * * *")
    assert next_run == datetime(2026, 1, 1, 12, 5, 0, tzinfo=UTC)


def test_cron_preserves_the_anchors_timezone():
    anchor = datetime(2026, 1, 1, 12, 3, 0, tzinfo=UTC)
    next_run = compute_next_run(ScheduleType.CRON, anchor=anchor, cron_expr="0 0 * * *")
    assert next_run is not None
    assert next_run.tzinfo is not None


def test_cron_missing_expr_degrades_to_never_due():
    anchor = datetime(2026, 1, 1, tzinfo=UTC)
    assert compute_next_run(ScheduleType.CRON, anchor=anchor, cron_expr=None) is None
    assert compute_next_run(ScheduleType.CRON, anchor=anchor, cron_expr="") is None


def test_webhook_is_never_time_due():
    anchor = datetime(2026, 1, 1, tzinfo=UTC)
    assert compute_next_run(ScheduleType.WEBHOOK, anchor=anchor) is None
