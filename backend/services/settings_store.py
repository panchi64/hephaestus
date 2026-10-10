"""Owner-scoped key/value operator preferences over the ``AppSetting`` table.

A tiny persisted store for small, non-secret preferences that don't warrant a bespoke
table (the local models directory is the first). Plain strings — structured/secret
config still lives in ``core/config.py`` (env) and the vault. Owner-scoped like every
record, so a per-user split later needs no rewrite.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Engine
from sqlmodel import Session, select

from core.config import get_settings
from core.db import in_session
from models._fields import utcnow
from models.app_setting import AppSetting
from runs import DEFAULT_CONTEXT_THRESHOLDS, ContextThresholds

# Conversation auto-compaction (agent/summarize.py) — the product's one pressure-driven
# context reduction: whether to fold a thread's older turns into a summary its own model
# writes once its footprint reaches `threshold` of the model's context window, expressed as a
# fraction (0.80 = 80%).
AUTO_COMPACT_ENABLED_KEY = "chat.auto_compact_enabled"
AUTO_COMPACT_THRESHOLD_KEY = "chat.auto_compact_threshold"

# The per-conversation work summary (services/work_summaries.py): how many minutes a thread
# must have been quiet before the background sweep writes an account of what the agent did
# in it. An operator setting rather than config-only because how long a pause has to be
# before it counts as *leaving* a thread is a property of how somebody works, and the band
# is either there when they come back or it is useless.
WORK_SUMMARY_IDLE_MINUTES_KEY = "chat.work_summary_idle_minutes"

# The ceiling on that idle window. A sanity bound, not a safety one: a window measured in
# days would mean the band never appears on a thread worked on in a normal week, which is
# indistinguishable from the feature being off — and switching it off is not what a number
# is for. A day is comfortably past any real "I stepped away" pause.
WORK_SUMMARY_IDLE_MINUTES_MAX = 1440

# The context gauge's severity boundaries (runs/events/ `ContextThresholds`): the two
# fullness fractions at which the ring under the composer turns amber and then red.
# Presentation, but not only presentation — the same `level` is what any overflow warning
# keys off — so it is stored and validated here rather than as a client-side constant.
CONTEXT_WARN_THRESHOLD_KEY = "chat.context_warn_threshold"
CONTEXT_ALERT_THRESHOLD_KEY = "chat.context_alert_threshold"

# The agent's per-turn model-request ceiling (agent/turn.py's `UsageLimits`): the
# operator's runtime override of `agent_request_limit`. Every model round-trip spends
# one, so this is what a tool-heavy turn actually runs out of.
AGENT_REQUEST_LIMIT_KEY = "chat.agent_request_limit"

# The run substrate's inactivity watchdog (runs/registry.py): the operator's runtime
# override of `run_inactivity_timeout_s` — how long a run may go without emitting an
# event before it is stopped. Long generations (a big file write, a slow first token)
# need more than the 120s default, so this is what the operator tunes to keep a turn
# alive. Seconds; the config default (or None = disabled) applies when unset.
INACTIVITY_TIMEOUT_KEY = "chat.inactivity_timeout_s"

# The run substrate's wall-clock bound (runs/registry.py): the operator's runtime override
# of `run_wall_clock_timeout_s` — how long a run may take in total, however busy it is.
# Off by default (see the config note): the useful case is a run that keeps emitting and
# so never trips the inactivity watchdog, which nothing else stops. Unlike every other
# key here, "unset" and "off" are different states — an empty stored value means the
# operator turned it off, which must not fall back to the config default.
WALL_CLOCK_TIMEOUT_KEY = "chat.wall_clock_timeout_s"
_DISABLED = ""

# How many sub-agents may be running for the operator at once (services/subagents/). Unset
# means no cap, which is the default and the honest one: the model launches these to *save*
# the operator time, and a number chosen in advance without knowing what the work splits
# into would mostly be wrong. The setting exists for the operator who has watched it launch
# six at a time and decided that is not what they want their evening spent on. Like the wall
# clock, "unset" and "off" are the same state here rather than different ones — there is no
# way to say "no sub-agents at all" with a number, because that is what switching the tool
# off in the catalog is for.
SUBAGENT_LIMIT_KEY = "chat.subagent_max_concurrent"

# Offline mode (services/offline.py) persists the operator's two switches here as
# "true"/"false" strings: the manual force-offline toggle and the auto-detect master
# switch. Policy, not a secret — stored in the clear like every other app preference.
OFFLINE_MANUAL_KEY = "offline.manual"
OFFLINE_AUTO_KEY = "offline.auto"

# The operator's disabled-tool set (services/tool_policy.py, AE-3.3), stored as a JSON
# list of namespaced tool names. Policy, not content — in the clear like the rest.
DISABLED_TOOLS_KEY = "tools.disabled"

# The project the operator is currently working in (services/projects), as a project id.
# Unset or empty means no project is active — which is not the same as "nothing is
# visible": unfiled rows are visible in every scope, so an unset key simply means the
# operator sees exactly what they saw before projects existed. A selection, not a
# session: it survives a reload and a vault lock, which is what a *workspace* implies.
ACTIVE_PROJECT_KEY = "projects.active"


class SettingsStore:
    def __init__(self, db_engine: Engine) -> None:
        self._db = db_engine

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
        """Read several (owner, key) preferences in one query — for a getter that needs a group
        of related keys (e.g. auto-compaction's pair) without paying a round-trip per key.
        Absent keys are simply omitted from the result; the caller applies its own defaults.

        Only the keys asked for are read, so a key this codebase no longer knows about is
        inert: a retired setting's row can be left in place rather than migrated away."""

        def work(session: Session) -> dict[str, str]:
            rows = session.exec(
                select(AppSetting)
                .where(AppSetting.owner_id == owner_id)
                .where(AppSetting.key.in_(keys))  # type: ignore[attr-defined]
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


async def get_agent_request_limit(store: SettingsStore, owner_id: str) -> int:
    """The operator's per-turn model-request ceiling — the runtime override if set (and
    valid), else the config default. Unlike the token caps this one is floored at 1, not
    0: a turn allowed zero model requests could never answer at all, so a stored 0 (or a
    corrupted value) falls back to the default rather than bricking every turn.

    This is the *effective* number, for the surface that shows the operator what their
    setting is. A turn composes with :func:`get_agent_request_limit_override` instead,
    because it has to tell an answer the operator chose from one nobody did."""
    return await get_agent_request_limit_override(store, owner_id) or (
        get_settings().agent_request_limit
    )


async def get_agent_request_limit_override(
    store: SettingsStore, owner_id: str
) -> int | None:
    """The operator's per-turn ceiling **only if they set one**, else None.

    The distinction is load-bearing where a mode carries a floor of its own
    (``services/modes.py``): a floor may raise a default nobody chose, but it must not
    overrule a number the operator explicitly lowered — otherwise the settings page says
    10 while a research turn runs at 60. Collapsing "unset" into the config default here
    would make those two cases indistinguishable downstream."""
    raw = await store.get(owner_id, AGENT_REQUEST_LIMIT_KEY)
    if raw is None:
        return None
    # A stored 0, negative or non-numeric value is corruption rather than intent, and
    # reads as unset — the same fallback every other getter in this module makes, phrased
    # as "the operator has not given us a usable number".
    value = _positive_int_or(raw, 0)
    return value or None


async def set_agent_request_limit(store: SettingsStore, owner_id: str, value: int) -> int:
    """Persist the operator's per-turn model-request ceiling. Returns the stored value.
    The ``ge=1`` body field at the route is what rejects a nonsensical one; a stray
    sub-1 value stored here would simply be ignored by the getter."""
    await store.set(owner_id, AGENT_REQUEST_LIMIT_KEY, str(value))
    return value


async def get_subagent_limit(store: SettingsStore, owner_id: str) -> int | None:
    """How many sub-agents may run at once, or ``None`` for no cap.

    ``None`` is the default and is not a fallback from a missing config value — there is no
    config default to fall back to, because "however many the work splits into" is the
    intended behaviour rather than an unset one. A stored 0, negative or non-numeric value
    is corruption rather than intent and reads as no cap, which is the same direction every
    other getter here falls: back to how the product behaves out of the box, rather than to
    a restriction the operator never asked for.
    """
    value = _positive_int_or(await store.get(owner_id, SUBAGENT_LIMIT_KEY), 0)
    return value or None


async def set_subagent_limit(
    store: SettingsStore, owner_id: str, value: int | None
) -> int | None:
    """Persist the cap, or ``None`` to remove it. Returns the stored value. The ``ge=1``
    body field at the route rejects a nonsensical number; ``None`` is a deliberate choice
    and the route distinguishes it from an omission before calling this."""
    await store.set(owner_id, SUBAGENT_LIMIT_KEY, _DISABLED if value is None else str(value))
    return value


async def get_inactivity_timeout(store: SettingsStore, owner_id: str) -> float | None:
    """The operator's inactivity timeout in seconds — the runtime override if set (and
    valid), else the config default. The default may itself be ``None`` (watchdog
    disabled), which is a meaningful value to return, not a corruption. A stored 0,
    negative, or non-numeric value falls back to the default rather than disabling the
    watchdog by accident."""
    raw = await store.get(owner_id, INACTIVITY_TIMEOUT_KEY)
    return _positive_float_or(raw, get_settings().run_inactivity_timeout_s)


async def set_inactivity_timeout(
    store: SettingsStore, owner_id: str, value: float
) -> float:
    """Persist the operator's inactivity timeout in seconds. Returns the stored value.
    The ``gt=0`` body field at the route rejects a nonsensical one; a stray non-positive
    value stored here would simply be ignored by the getter."""
    await store.set(owner_id, INACTIVITY_TIMEOUT_KEY, str(value))
    return value


async def get_wall_clock_timeout(store: SettingsStore, owner_id: str) -> float | None:
    """The operator's wall-clock bound in seconds — the runtime override if set (and
    valid), else the config default (itself ``None``, i.e. no bound, unless the deploy
    sets one).

    The empty stored value is the one case that must *not* fall back: it is how the
    operator says "no bound", which is indistinguishable from the default only while the
    default happens to be ``None`` too. A 0, negative, or non-numeric value is corruption
    rather than intent, so it falls back like everywhere else in this module."""
    raw = await store.get(owner_id, WALL_CLOCK_TIMEOUT_KEY)
    if raw == _DISABLED:
        return None
    return _positive_float_or(raw, get_settings().run_wall_clock_timeout_s)


async def set_wall_clock_timeout(
    store: SettingsStore, owner_id: str, value: float | None
) -> float | None:
    """Persist the operator's wall-clock bound in seconds, or ``None`` to remove it.
    Returns the stored value. The ``gt=0`` body field at the route rejects a nonsensical
    number; ``None`` is a deliberate choice, not an omission, and the route distinguishes
    the two before calling this."""
    await store.set(
        owner_id, WALL_CLOCK_TIMEOUT_KEY, _DISABLED if value is None else str(value)
    )
    return value


def _int_or(raw: str | None, default: int) -> int:
    """A non-negative int from a stored string, else the default (a corrupted value can't
    silently flip the setting to something nonsensical)."""
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def _positive_int_or(raw: str | None, default: int) -> int:
    """:func:`_int_or` for a setting where 0 is meaningless rather than merely minimal —
    the floor is 1, so a stored 0 falls back to the default like any other bad value."""
    value = _int_or(raw, default)
    return value if value >= 1 else default


def _bounded_int_or(raw: str | None, default: int, *, maximum: int, minimum: int = 0) -> int:
    """:func:`_int_or` for a setting that is capped as well as floored — a stored value
    outside the range is corruption or a client that skipped the route's bound, and falls
    back rather than being silently clamped to a number the operator never chose.

    ``minimum`` defaults to 0, which is :func:`_int_or`'s own floor, so a caller only names
    it for a setting where 0 is meaningless rather than merely minimal."""
    value = _int_or(raw, default)
    return value if minimum <= value <= maximum else default


@dataclass(frozen=True)
class AutoCompactSettings:
    """The operator's effective conversation auto-compaction preferences. ``threshold`` is
    a fraction of the model's context window, not a percentage — the UI presents it as one,
    but the wire and the store carry the same 0–1 quantity the context meter already uses."""

    enabled: bool
    threshold: float


def _float_or(raw: str | None, default: float) -> float:
    """A float in ``(0, 1]`` from a stored string, else the default — :func:`_int_or` for
    the one setting that is a fraction. Both bounds matter: 0 (or a negative) would fire
    compaction on an empty thread, and above 1 it could never fire at all, so a corrupted
    value falls back rather than silently disabling or thrashing the feature."""
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if 0 < value <= 1 else default


def _positive_float_or(raw: str | None, default: float | None) -> float | None:
    """A positive float (seconds) from a stored string, else the default — the
    :func:`_float_or` counterpart for a setting whose value is unbounded above and whose
    default may legitimately be ``None`` (the watchdog disabled). A stored 0, negative, or
    non-numeric value falls back to the default rather than silently disabling a bound."""
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _bool_or(raw: str | None, default: bool) -> bool:
    """``True``/``False`` from a stored ``"true"``/``"false"`` string, else the default — so a
    corrupted/legacy value falls back to the default instead of silently reading as ``False``
    (the failure mode of a bare ``raw == "true"``), mirroring :func:`_int_or`."""
    if raw == "true":
        return True
    if raw == "false":
        return False
    return default


def resolve_compaction_enabled(override: bool | None, global_enabled: bool) -> bool:
    """A conversation's effective compaction on/off: its stored override when set, else the
    operator's global default. The one place this precedence lives, so the per-thread toggle's
    displayed state and the turn's actual behavior can't drift apart."""
    return global_enabled if override is None else override


async def get_auto_compact(store: SettingsStore, owner_id: str) -> AutoCompactSettings:
    """The operator's effective conversation auto-compaction settings — runtime overrides
    where set (and valid), else the config defaults. One batched read for the group."""
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
    await store.set(
        owner_id, AUTO_COMPACT_ENABLED_KEY, "true" if settings.enabled else "false"
    )
    await store.set(owner_id, AUTO_COMPACT_THRESHOLD_KEY, str(settings.threshold))
    return settings


async def get_work_summary_idle_minutes(store: SettingsStore, owner_id: str) -> int:
    """How long a thread must be quiet before the sweep writes its work summary — the
    stored value where set (and valid), else the config default.

    A single key rather than a group, so it is read on its own rather than batched: the
    sweeper asks for exactly this on every pass, and the compaction group next door is
    read by a different caller for a different reason. ``minimum=1`` because a zero-minute
    window is not a faster setting but a broken one — every thread would be a candidate the
    instant its turn finished, and the sweep would summarize conversations mid-conversation.
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
    """The operator's context-gauge severity boundaries — the stored pair where set (and
    valid), else the defaults. One batched read, like auto-compaction's pair.

    The fallback is all-or-nothing rather than per-field, because the two are only
    meaningful together: a stored ``warn`` of 0.95 read alongside a defaulted ``alert``
    of 0.9 is an inverted pair that no operator ever chose. So a value that is out of
    range, unparseable, or (with its partner) out of order sends *both* back to the
    defaults — a gauge with the wrong boundaries is worse than one with the stock pair,
    since the operator has no way to tell it is miscalibrated by looking at it."""
    values = await store.get_many(
        owner_id, (CONTEXT_WARN_THRESHOLD_KEY, CONTEXT_ALERT_THRESHOLD_KEY)
    )
    try:
        return ContextThresholds(
            warn=_fraction(values.get(CONTEXT_WARN_THRESHOLD_KEY)),
            alert=_fraction(values.get(CONTEXT_ALERT_THRESHOLD_KEY)),
        )
    except (TypeError, ValueError):
        # `ValueError` covers pydantic's ValidationError (its base) — the ordering
        # invariant — as well as an unparseable string; `TypeError` covers an absent key.
        return DEFAULT_CONTEXT_THRESHOLDS


def _fraction(raw: str | None) -> float:
    """A stored fraction as a float, raising on anything unusable so the caller can fall
    back to the whole default pair rather than to a half-defaulted one."""
    if raw is None:
        raise TypeError("unset")
    return float(raw)


async def set_context_thresholds(
    store: SettingsStore, owner_id: str, thresholds: ContextThresholds
) -> ContextThresholds:
    """Persist the operator's context-gauge boundaries. Returns the stored pair.

    Takes a constructed ``ContextThresholds``, so the ordering invariant has already been
    checked by the one place that owns it — this can't write a pair the getter would then
    reject and silently replace with the defaults."""
    await store.set(owner_id, CONTEXT_WARN_THRESHOLD_KEY, str(thresholds.warn))
    await store.set(owner_id, CONTEXT_ALERT_THRESHOLD_KEY, str(thresholds.alert))
    return thresholds
