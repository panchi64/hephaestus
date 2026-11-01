"""Owner-scoped key/value operator preferences over the ``AppSetting`` table.

A tiny persisted store for small, non-secret preferences that don't warrant a bespoke
table (the local models directory is the first). Plain strings — structured/secret
config still lives in ``core/config.py`` (env) and the vault. Owner-scoped like every
record, so a per-user split later needs no rewrite.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Engine
from sqlmodel import Session, col, select

from core.config import Settings, get_settings
from core.db import in_session
from models._fields import utcnow
from models.app_setting import AppSetting
from runs import DEFAULT_CONTEXT_THRESHOLDS, ContextThresholds

# Conversation auto-compaction (agent/summarize.py): whether to fold a thread's older turns
# into a summary once its footprint reaches `threshold`, a fraction (0.80 = 80%) of the
# model's context window.
AUTO_COMPACT_ENABLED_KEY = "chat.auto_compact_enabled"
AUTO_COMPACT_THRESHOLD_KEY = "chat.auto_compact_threshold"

# Minutes a thread must be quiet before the background sweep writes its work summary
# (services/work_summaries.py); an operator setting because what counts as leaving a thread
# depends on how somebody works.
WORK_SUMMARY_IDLE_MINUTES_KEY = "chat.work_summary_idle_minutes"

# Ceiling on that window: a sanity bound, since days-long windows are indistinguishable
# from the feature being off.
WORK_SUMMARY_IDLE_MINUTES_MAX = 1440

# The context gauge's two fullness fractions (amber, then red; `ContextThresholds`). Stored
# and validated here because overflow warnings key off the same `level`.
CONTEXT_WARN_THRESHOLD_KEY = "chat.context_warn_threshold"
CONTEXT_ALERT_THRESHOLD_KEY = "chat.context_alert_threshold"

# Operator override of `run_inactivity_timeout_s` (runs/registry.py): seconds a run may go
# without emitting an event before it is stopped. Config default (or None = disabled)
# applies when unset.
INACTIVITY_TIMEOUT_KEY = "chat.inactivity_timeout_s"

# Operator override of `run_wall_clock_timeout_s` (runs/registry.py): total seconds a run
# may take, however busy; it stops a run that keeps emitting and never trips the inactivity
# watchdog. Unlike other keys, unset and off differ: an empty stored value means off and
# must not fall back to the config default.
WALL_CLOCK_TIMEOUT_KEY = "chat.wall_clock_timeout_s"
_DISABLED = ""

# How long a turn waits for a model's first streamed token (`first_token_timeout_s`); a
# local model reading a long cold prompt can be silent for minutes.
FIRST_TOKEN_TIMEOUT_KEY = "chat.first_token_timeout_s"

# Sub-agents running in one delegation tree at once (services/subagents/); unset means
# `DEFAULT_MAX_CONCURRENT` (8). Unset and off are the same state: disabling sub-agents is
# the catalog's tool switch, not a zero here.
SUBAGENT_LIMIT_KEY = "chat.subagent_max_concurrent"

# Offline mode (services/offline.py): the manual force-offline toggle and the auto-detect
# master switch, as "true"/"false" strings.
OFFLINE_MANUAL_KEY = "offline.manual"
OFFLINE_AUTO_KEY = "offline.auto"

# The operator's disabled-tool set (services/tool_policy.py), a JSON list of namespaced
# tool names.
DISABLED_TOOLS_KEY = "tools.disabled"

# The active project id (services/projects). Unset or empty means none is active, and
# unfiled rows are visible in every scope. A selection that survives a reload and a lock.
ACTIVE_PROJECT_KEY = "projects.active"


class SettingsStore:
    def __init__(self, db_engine: Engine) -> None:
        self._db: Engine = db_engine

    async def get(self, owner_id: str, key: str, default: str | None = None) -> str | None:
        def work(session: Session) -> str | None:
            row = session.exec(
                select(AppSetting)
                .where(AppSetting.owner_id == owner_id)
                .where(AppSetting.key == key)
            ).first()
            return row.value if row is not None else default

        return await in_session(self._db, work)

    async def get_many(self, owner_id: str, keys: tuple[str, ...]) -> dict[str, str]:
        """Read several (owner, key) preferences in one query. Absent keys are omitted; the
        caller applies its defaults. A retired setting's row is inert and can stay."""

        def work(session: Session) -> dict[str, str]:
            rows = session.exec(
                select(AppSetting)
                .where(AppSetting.owner_id == owner_id)
                .where(col(AppSetting.key).in_(keys))
            ).all()
            return {row.key: row.value for row in rows}

        return await in_session(self._db, work)

    async def set(self, owner_id: str, key: str, value: str) -> None:
        """Upsert one (owner, key) preference."""

        def work(session: Session) -> None:
            row = session.exec(
                select(AppSetting)
                .where(AppSetting.owner_id == owner_id)
                .where(AppSetting.key == key)
            ).first()
            if row is None:
                session.add(AppSetting(owner_id=owner_id, key=key, value=value))
            else:
                row.value = value
                row.updated_at = utcnow()
                session.add(row)

        await in_session(self._db, work)


async def get_subagent_limit(store: SettingsStore, owner_id: str) -> int | None:
    """The operator's cap on sub-agents at once, or ``None`` when unset (the launcher's
    default of 8). A stored 0, negative or non-numeric value is corruption and reads as
    unset.
    """
    value = _positive_int_or(await store.get(owner_id, SUBAGENT_LIMIT_KEY), 0)
    return value or None


async def set_subagent_limit(store: SettingsStore, owner_id: str, value: int | None) -> int | None:
    """Persist the cap, or ``None`` to remove it; the route rejects a number below 1 and
    distinguishes ``None`` from an omission."""
    await store.set(owner_id, SUBAGENT_LIMIT_KEY, _DISABLED if value is None else str(value))
    return value


async def get_inactivity_timeout(store: SettingsStore, owner_id: str) -> float | None:
    """The inactivity timeout in seconds: the valid override, else the config default
    (which may be ``None``, watchdog disabled). A stored 0, negative or non-numeric value
    falls back rather than disabling the watchdog."""
    raw = await store.get(owner_id, INACTIVITY_TIMEOUT_KEY)
    return _positive_float_or(raw, get_settings().run_inactivity_timeout_s)


async def set_inactivity_timeout(store: SettingsStore, owner_id: str, value: float) -> float:
    """Persist the inactivity timeout in seconds; the route rejects non-positive values."""
    await store.set(owner_id, INACTIVITY_TIMEOUT_KEY, str(value))
    return value


async def get_wall_clock_timeout(store: SettingsStore, owner_id: str) -> float | None:
    """The wall-clock bound in seconds: the valid override, else the config default
    (``None``, no bound, unless the deploy sets one).

    The empty stored value must not fall back: it is the operator's "no bound". A 0,
    negative or non-numeric value is corruption and falls back."""
    raw = await store.get(owner_id, WALL_CLOCK_TIMEOUT_KEY)
    if raw == _DISABLED:
        return None
    return _positive_float_or(raw, get_settings().run_wall_clock_timeout_s)


async def set_wall_clock_timeout(
    store: SettingsStore, owner_id: str, value: float | None
) -> float | None:
    """Persist the wall-clock bound in seconds, or ``None`` to remove it; the route rejects
    non-positive numbers and distinguishes ``None`` from an omission."""
    await store.set(owner_id, WALL_CLOCK_TIMEOUT_KEY, _DISABLED if value is None else str(value))
    return value


async def get_first_token_timeout(store: SettingsStore, owner_id: str) -> float | None:
    """The operator's first-token wait in seconds, else the config default (which a deploy
    may set to None, leaving that wait to the inactivity watchdog)."""
    raw = await store.get(owner_id, FIRST_TOKEN_TIMEOUT_KEY)
    return _positive_float_or(raw, get_settings().first_token_timeout_s)


async def set_first_token_timeout(store: SettingsStore, owner_id: str, value: float) -> float:
    await store.set(owner_id, FIRST_TOKEN_TIMEOUT_KEY, str(value))
    return value


class TurnTuning:
    """The operator's bounds on how a turn waits; the slice of the settings store a run reads."""

    def __init__(self, store: SettingsStore) -> None:
        self._store: SettingsStore = store

    async def apply(self, settings: Settings, owner_id: str) -> Settings:
        return settings.model_copy(
            update={
                "first_token_timeout_s": await get_first_token_timeout(self._store, owner_id),
            }
        )


def _int_or(raw: str | None, default: int) -> int:
    """A non-negative int from a stored string, else the default."""
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def _positive_int_or(raw: str | None, default: int) -> int:
    """:func:`_int_or` with a floor of 1, for a setting where 0 is meaningless."""
    value = _int_or(raw, default)
    return value if value >= 1 else default


def _bounded_int_or(raw: str | None, default: int, *, maximum: int, minimum: int = 0) -> int:
    """:func:`_int_or` with a cap: a value outside the range falls back rather than being
    clamped to a number the operator never chose."""
    value = _int_or(raw, default)
    return value if minimum <= value <= maximum else default


@dataclass(frozen=True)
class AutoCompactSettings:
    """The effective auto-compaction preferences. ``threshold`` is a 0-1 fraction of the
    context window, the same quantity the context meter uses; the UI shows a percentage."""

    enabled: bool
    threshold: float


def _float_or(raw: str | None, default: float) -> float:
    """A float in ``(0, 1]`` from a stored string, else the default. Zero would compact an
    empty thread and above 1 compaction could never fire."""
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if 0 < value <= 1 else default


def _positive_float_or(raw: str | None, default: float | None) -> float | None:
    """A positive float (seconds) from a stored string, else the default, which may be
    ``None`` (bound disabled). A stored 0, negative or non-numeric value falls back."""
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _bool_or(raw: str | None, default: bool) -> bool:
    """A bool from a stored ``"true"``/``"false"`` string, else the default, so a corrupt
    value does not read as ``False`` (as a bare ``raw == "true"`` would)."""
    if raw == "true":
        return True
    if raw == "false":
        return False
    return default


def resolve_compaction_enabled(override: bool | None, global_enabled: bool) -> bool:
    """A conversation's effective compaction on/off: its override, else the global default.
    The one home of this precedence, so the toggle and the turn agree."""
    return global_enabled if override is None else override


async def get_auto_compact(store: SettingsStore, owner_id: str) -> AutoCompactSettings:
    """The effective auto-compaction settings: valid overrides, else config defaults."""
    cfg = get_settings()
    values = await store.get_many(owner_id, (AUTO_COMPACT_ENABLED_KEY, AUTO_COMPACT_THRESHOLD_KEY))
    return AutoCompactSettings(
        enabled=_bool_or(values.get(AUTO_COMPACT_ENABLED_KEY), cfg.auto_compact_enabled),
        threshold=_float_or(values.get(AUTO_COMPACT_THRESHOLD_KEY), cfg.auto_compact_threshold),
    )


async def set_auto_compact(
    store: SettingsStore, owner_id: str, settings: AutoCompactSettings
) -> AutoCompactSettings:
    """Persist the operator's auto-compaction preferences. Returns the stored settings."""
    await store.set(owner_id, AUTO_COMPACT_ENABLED_KEY, "true" if settings.enabled else "false")
    await store.set(owner_id, AUTO_COMPACT_THRESHOLD_KEY, str(settings.threshold))
    return settings


async def get_work_summary_idle_minutes(store: SettingsStore, owner_id: str) -> int:
    """How long a thread must be quiet before the sweep writes its work summary: the valid
    stored value, else the config default.

    ``minimum=1`` because a zero window would make every thread a candidate the instant its
    turn finished, summarizing conversations mid-conversation.
    """
    raw = await store.get(owner_id, WORK_SUMMARY_IDLE_MINUTES_KEY)
    return _bounded_int_or(
        raw,
        get_settings().work_summary_idle_minutes,
        minimum=1,
        maximum=WORK_SUMMARY_IDLE_MINUTES_MAX,
    )


async def set_work_summary_idle_minutes(store: SettingsStore, owner_id: str, minutes: int) -> int:
    """Persist the operator's idle window. Returns the stored value."""
    await store.set(owner_id, WORK_SUMMARY_IDLE_MINUTES_KEY, str(minutes))
    return minutes


async def get_context_thresholds(store: SettingsStore, owner_id: str) -> ContextThresholds:
    """The context-gauge severity boundaries: the stored pair if valid, else the defaults.

    The fallback is all-or-nothing, since a stored ``warn`` of 0.95 beside a defaulted
    ``alert`` of 0.9 is an inverted pair no operator chose. An out-of-range, unparseable or
    out-of-order value sends both back to the defaults; a silently miscalibrated gauge is
    worse than the stock one."""
    values = await store.get_many(
        owner_id, (CONTEXT_WARN_THRESHOLD_KEY, CONTEXT_ALERT_THRESHOLD_KEY)
    )
    try:
        return ContextThresholds(
            warn=_fraction(values.get(CONTEXT_WARN_THRESHOLD_KEY)),
            alert=_fraction(values.get(CONTEXT_ALERT_THRESHOLD_KEY)),
        )
    except TypeError, ValueError:
        # `ValueError` covers pydantic's ValidationError (the ordering invariant) and an
        # unparseable string; `TypeError` covers an absent key.
        return DEFAULT_CONTEXT_THRESHOLDS


def _fraction(raw: str | None) -> float:
    """A stored fraction as a float, raising on anything unusable so the caller falls back
    to the whole default pair."""
    if raw is None:
        raise TypeError("unset")
    return float(raw)


async def set_context_thresholds(
    store: SettingsStore, owner_id: str, thresholds: ContextThresholds
) -> ContextThresholds:
    """Persist the operator's context-gauge boundaries. Returns the stored pair.

    Takes a constructed ``ContextThresholds``, whose ordering invariant is already checked,
    so this cannot write a pair the getter would reject."""
    await store.set(owner_id, CONTEXT_WARN_THRESHOLD_KEY, str(thresholds.warn))
    await store.set(owner_id, CONTEXT_ALERT_THRESHOLD_KEY, str(thresholds.alert))
    return thresholds
