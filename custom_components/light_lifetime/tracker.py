"""Persistent lifetime tracking for light entities.

The tracker owns a single JSON document in ``.storage`` that survives restarts
and is entirely independent of the recorder, so counters are unaffected by
``purge_keep_days``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    EVENT_HOMEASSISTANT_STOP,
    EVENT_STATE_CHANGED,
    STATE_ON,
    STATE_UNAVAILABLE,
)
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    AGGREGATE_MODELS,
    AGGREGATE_PLATFORMS,
    ATTR_DROPOUTS,
    ATTR_FIRST_SEEN,
    ATTR_FIRST_SEEN_SOURCE,
    ATTR_ON_SECONDS,
    ATTR_ON_SINCE,
    ATTR_TRACKED_SINCE,
    CONF_COUNT_DOWNTIME,
    CONF_EXCLUDE_AGGREGATES,
    CONF_EXCLUDED_ENTITIES,
    DEFAULT_COUNT_DOWNTIME,
    DEFAULT_EXCLUDE_AGGREGATES,
    EPOCH_SENTINEL_YEAR,
    HEARTBEAT_INTERVAL,
    SAVE_DELAY,
    SIGNAL_NEW_ENTITY,
    SIGNAL_REFRESH,
    SIGNAL_UPDATED,
    SOURCE_REGISTRY,
    SOURCE_UNKNOWN,
    STORAGE_KEY,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)

LIGHT_PREFIX = "light."


def _parse(value: Any) -> datetime | None:
    """Parse a stored ISO timestamp, tolerating nulls and malformed values."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    return dt_util.parse_datetime(str(value))


class LightLifetimeTracker:
    """Accumulates lifetime on-time and dropout counts for every light."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._store = Store[dict[str, Any]](hass, STORAGE_VERSION, STORAGE_KEY)
        self._data: dict[str, dict[str, Any]] = {}
        self._heartbeat: datetime | None = None
        self._unsubs: list[Any] = []

    # ------------------------------------------------------------------
    # Options
    # ------------------------------------------------------------------
    @property
    def _exclude_aggregates(self) -> bool:
        return self.entry.options.get(
            CONF_EXCLUDE_AGGREGATES, DEFAULT_EXCLUDE_AGGREGATES
        )

    @property
    def _count_downtime(self) -> bool:
        return self.entry.options.get(CONF_COUNT_DOWNTIME, DEFAULT_COUNT_DOWNTIME)

    @property
    def _excluded_entities(self) -> set[str]:
        return set(self.entry.options.get(CONF_EXCLUDED_ENTITIES, []))

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def async_setup(self) -> None:
        """Load persisted state and start listening."""
        stored = await self._store.async_load()
        if stored:
            self._data = stored.get("entities", {}) or {}
            self._heartbeat = _parse(stored.get("heartbeat"))

        self._close_stale_intervals()

        self._unsubs.append(
            self.hass.bus.async_listen(EVENT_STATE_CHANGED, self._handle_state_changed)
        )
        self._unsubs.append(
            self.hass.bus.async_listen(
                er.EVENT_ENTITY_REGISTRY_UPDATED, self._handle_registry_updated
            )
        )
        self._unsubs.append(
            self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STOP, self._handle_stop
            )
        )
        self._unsubs.append(
            async_track_time_interval(
                self.hass, self._handle_heartbeat, timedelta(seconds=HEARTBEAT_INTERVAL)
            )
        )
        self._unsubs.append(async_at_started(self.hass, self._handle_started))

    async def async_unload(self) -> None:
        """Detach listeners and flush pending writes."""
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        await self._async_flush()

    # ------------------------------------------------------------------
    # Startup reconciliation
    # ------------------------------------------------------------------
    @callback
    def _close_stale_intervals(self) -> None:
        """Settle "on" intervals left open by the previous run.

        With ``count_downtime`` disabled an open interval is truncated at the
        last heartbeat -- the last moment we know Home Assistant was running --
        so an outage (or an unclean kill, where no shutdown handler ran) does
        not get silently counted as on-time.
        """
        if self._count_downtime:
            return
        for entity_id, record in self._data.items():
            on_since = _parse(record.get(ATTR_ON_SINCE))
            if on_since is None:
                continue
            end = self._heartbeat
            if end is None or end < on_since:
                end = on_since
            self._accumulate(record, (end - on_since).total_seconds())
            record[ATTR_ON_SINCE] = None
            _LOGGER.debug("Truncated open interval for %s at %s", entity_id, end)

    @callback
    def _handle_started(self, _hass: HomeAssistant) -> None:
        """Discover lights once the state machine is populated."""
        now = dt_util.utcnow()
        discovered: list[str] = []
        for state in self.hass.states.async_all("light"):
            if not self._should_track(state.entity_id):
                continue
            if state.entity_id not in self._data:
                discovered.append(state.entity_id)
            record = self._ensure(state.entity_id)
            # Re-open an interval for anything already lit at startup.
            if state.state == STATE_ON and record.get(ATTR_ON_SINCE) is None:
                record[ATTR_ON_SINCE] = now.isoformat()
        self._schedule_save()
        for entity_id in discovered:
            async_dispatcher_send(self.hass, SIGNAL_NEW_ENTITY, entity_id)

    # ------------------------------------------------------------------
    # Event handling
    # ------------------------------------------------------------------
    @callback
    def _handle_state_changed(self, event: Event) -> None:
        entity_id: str | None = event.data.get("entity_id")
        if not entity_id or not entity_id.startswith(LIGHT_PREFIX):
            return
        new_state = event.data.get("new_state")
        if new_state is None:
            return  # entity removed
        old_state = event.data.get("old_state")
        old_value = old_state.state if old_state else None
        # Attribute-only churn (brightness/colour from adaptive lighting and the
        # like) carries no lifetime signal and is by far the bulk of the traffic.
        if old_value == new_state.state:
            return
        if not self._should_track(entity_id):
            return

        is_new = entity_id not in self._data
        record = self._ensure(entity_id)
        self._apply_transition(record, old_value, new_state.state, event.time_fired)
        self._schedule_save()

        if is_new:
            async_dispatcher_send(self.hass, SIGNAL_NEW_ENTITY, entity_id)
        else:
            async_dispatcher_send(self.hass, SIGNAL_UPDATED, entity_id)

    @callback
    def _apply_transition(
        self,
        record: dict[str, Any],
        old_value: str | None,
        new_value: str,
        when: datetime,
    ) -> None:
        was_on = old_value == STATE_ON
        is_on = new_value == STATE_ON

        if was_on and not is_on:
            self._close_interval(record, when)
        elif is_on and not was_on:
            record[ATTR_ON_SINCE] = when.isoformat()

        # `old_value is None` means the entity was just added to the state
        # machine, which happens on every restart -- not a real dropout.
        if new_value == STATE_UNAVAILABLE and old_value not in (
            None,
            STATE_UNAVAILABLE,
        ):
            record[ATTR_DROPOUTS] = int(record.get(ATTR_DROPOUTS, 0)) + 1

    @callback
    def _handle_registry_updated(self, event: Event) -> None:
        """Follow entity_id renames so counters are not orphaned."""
        if event.data.get("action") != "update":
            return
        changes = event.data.get("changes") or {}
        if "entity_id" not in changes:
            return
        old_id = changes["entity_id"]
        new_id = event.data.get("entity_id")
        if not old_id or not new_id or old_id not in self._data:
            return
        self._data[new_id] = self._data.pop(old_id)
        self._schedule_save()
        _LOGGER.debug("Migrated lifetime counters %s -> %s", old_id, new_id)

    async def _handle_heartbeat(self, _now: datetime) -> None:
        self._heartbeat = dt_util.utcnow()
        self._schedule_save()
        # Nudge sensors so open intervals tick forward in the UI and in
        # long-term statistics rather than only moving on transitions.
        async_dispatcher_send(self.hass, SIGNAL_REFRESH)

    async def _handle_stop(self, _event: Event) -> None:
        now = dt_util.utcnow()
        if not self._count_downtime:
            for record in self._data.values():
                self._close_interval(record, now)
        self._heartbeat = now
        await self._async_flush()

    # ------------------------------------------------------------------
    # Accumulation helpers
    # ------------------------------------------------------------------
    @callback
    def _accumulate(self, record: dict[str, Any], seconds: float) -> None:
        if seconds <= 0:
            return
        record[ATTR_ON_SECONDS] = float(record.get(ATTR_ON_SECONDS, 0.0)) + seconds

    @callback
    def _close_interval(self, record: dict[str, Any], end: datetime) -> None:
        on_since = _parse(record.get(ATTR_ON_SINCE))
        if on_since is None:
            return
        self._accumulate(record, (end - on_since).total_seconds())
        record[ATTR_ON_SINCE] = None

    # ------------------------------------------------------------------
    # Entity selection
    # ------------------------------------------------------------------
    @callback
    def _should_track(self, entity_id: str) -> bool:
        if entity_id in self._excluded_entities:
            return False
        if not self._exclude_aggregates:
            return True
        entry = er.async_get(self.hass).async_get(entity_id)
        if entry is None:
            return True
        if entry.platform in AGGREGATE_PLATFORMS:
            return False
        if entry.device_id:
            device = dr.async_get(self.hass).async_get(entry.device_id)
            if device and (device.model or "").strip().lower() in AGGREGATE_MODELS:
                return False
        return True

    @callback
    def _resolve_first_seen(self, entity_id: str) -> tuple[str | None, str]:
        """Return a trustworthy creation timestamp, or None.

        Home Assistant backfilled ``created_at`` into registry entries that
        predate the field using the Unix epoch. Those sentinels are reported as
        unknown rather than dressed up as a real date.
        """
        entry = er.async_get(self.hass).async_get(entity_id)
        candidates: list[Any] = []
        if entry is not None:
            candidates.append(getattr(entry, "created_at", None))
            if entry.device_id:
                device = dr.async_get(self.hass).async_get(entry.device_id)
                if device is not None:
                    candidates.append(getattr(device, "created_at", None))
        for value in candidates:
            if isinstance(value, datetime) and value.year > EPOCH_SENTINEL_YEAR:
                return dt_util.as_utc(value).isoformat(), SOURCE_REGISTRY
        return None, SOURCE_UNKNOWN

    @callback
    def _ensure(self, entity_id: str) -> dict[str, Any]:
        record = self._data.get(entity_id)
        if record is None:
            first_seen, source = self._resolve_first_seen(entity_id)
            record = {
                ATTR_ON_SECONDS: 0.0,
                ATTR_DROPOUTS: 0,
                ATTR_FIRST_SEEN: first_seen,
                ATTR_FIRST_SEEN_SOURCE: source,
                ATTR_TRACKED_SINCE: dt_util.utcnow().isoformat(),
                ATTR_ON_SINCE: None,
            }
            self._data[entity_id] = record
            _LOGGER.debug("Tracking %s (first_seen=%s)", entity_id, source)
        return record

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    @callback
    def _serialize(self) -> dict[str, Any]:
        return {
            "entities": self._data,
            "heartbeat": (self._heartbeat or dt_util.utcnow()).isoformat(),
        }

    @callback
    def _schedule_save(self) -> None:
        self._store.async_delay_save(self._serialize, SAVE_DELAY)

    async def _async_flush(self) -> None:
        await self._store.async_save(self._serialize())

    # ------------------------------------------------------------------
    # Public read API (used by sensors and services)
    # ------------------------------------------------------------------
    @callback
    def tracked_entities(self) -> list[str]:
        return list(self._data)

    @callback
    def on_seconds(self, entity_id: str) -> float:
        """Lifetime on-time including any interval currently open."""
        record = self._data.get(entity_id)
        if record is None:
            return 0.0
        total = float(record.get(ATTR_ON_SECONDS, 0.0))
        on_since = _parse(record.get(ATTR_ON_SINCE))
        if on_since is not None:
            total += max(0.0, (dt_util.utcnow() - on_since).total_seconds())
        return total

    @callback
    def dropouts(self, entity_id: str) -> int:
        record = self._data.get(entity_id)
        return int(record.get(ATTR_DROPOUTS, 0)) if record else 0

    @callback
    def first_seen(self, entity_id: str) -> datetime | None:
        record = self._data.get(entity_id)
        return _parse(record.get(ATTR_FIRST_SEEN)) if record else None

    @callback
    def first_seen_source(self, entity_id: str) -> str:
        record = self._data.get(entity_id)
        return record.get(ATTR_FIRST_SEEN_SOURCE, SOURCE_UNKNOWN) if record else SOURCE_UNKNOWN

    @callback
    def tracked_since(self, entity_id: str) -> datetime | None:
        record = self._data.get(entity_id)
        return _parse(record.get(ATTR_TRACKED_SINCE)) if record else None

    @callback
    def is_on(self, entity_id: str) -> bool:
        record = self._data.get(entity_id)
        return bool(record and record.get(ATTR_ON_SINCE))

    # ------------------------------------------------------------------
    # Mutations (services)
    # ------------------------------------------------------------------
    @callback
    def reset(self, entity_id: str) -> None:
        """Zero an entity's counters -- use after physically replacing a bulb."""
        now = dt_util.utcnow()
        state = self.hass.states.get(entity_id)
        self._data[entity_id] = {
            ATTR_ON_SECONDS: 0.0,
            ATTR_DROPOUTS: 0,
            ATTR_FIRST_SEEN: now.isoformat(),
            ATTR_FIRST_SEEN_SOURCE: SOURCE_REGISTRY,
            ATTR_TRACKED_SINCE: now.isoformat(),
            ATTR_ON_SINCE: now.isoformat()
            if state is not None and state.state == STATE_ON
            else None,
        }
        self._schedule_save()
        async_dispatcher_send(self.hass, SIGNAL_UPDATED, entity_id)

    @callback
    def set_values(
        self,
        entity_id: str,
        on_hours: float | None = None,
        dropouts: int | None = None,
        first_seen: datetime | None = None,
    ) -> None:
        """Seed counters, e.g. from a known install date or a prior estimate."""
        record = self._ensure(entity_id)
        if on_hours is not None:
            record[ATTR_ON_SECONDS] = float(on_hours) * 3600.0
            if record.get(ATTR_ON_SINCE):
                # Re-anchor so the open interval is not double-counted.
                record[ATTR_ON_SINCE] = dt_util.utcnow().isoformat()
        if dropouts is not None:
            record[ATTR_DROPOUTS] = int(dropouts)
        if first_seen is not None:
            record[ATTR_FIRST_SEEN] = dt_util.as_utc(first_seen).isoformat()
            record[ATTR_FIRST_SEEN_SOURCE] = SOURCE_REGISTRY
        self._schedule_save()
        async_dispatcher_send(self.hass, SIGNAL_UPDATED, entity_id)
