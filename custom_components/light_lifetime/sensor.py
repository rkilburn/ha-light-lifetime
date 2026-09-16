"""Sensor entities exposing lifetime statistics for each tracked light."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device import async_entity_id_to_device
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import (
    ATTR_FIRST_SEEN_SOURCE,
    CONF_SENSORS,
    CONF_SUMMARY_SENSORS,
    DEFAULT_SENSORS,
    DEFAULT_SUMMARY_SENSORS,
    ATTR_TRACKED_SINCE,
    DOMAIN,
    SENSOR_KEYS,
    SIGNAL_NEW_ENTITY,
    SIGNAL_REFRESH,
    SIGNAL_RENAMED,
    SIGNAL_UPDATED,
    SOURCE_UNKNOWN,
)
from .tracker import LightLifetimeTracker

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensors for every tracked light, now and in the future."""
    tracker: LightLifetimeTracker = hass.data[DOMAIN][entry.entry_id]
    known: set[str] = set()
    keys = _enabled_keys(entry)

    @callback
    def _add(entity_id: str) -> None:
        if entity_id in known:
            return
        known.add(entity_id)
        async_add_entities(
            [SENSOR_TYPES[key](tracker, entity_id) for key in keys]
        )

    # The ledger keeps history for lights that are no longer tracked (so their
    # counters survive being re-included later), but only currently-tracked
    # lights get entities.
    if entry.options.get(CONF_SUMMARY_SENSORS, DEFAULT_SUMMARY_SENSORS):
        async_add_entities(
            [
                TotalOnHoursSensor(tracker, entry),
                LightsTrackedSensor(tracker, entry),
                LightsOnSensor(tracker, entry),
                TotalDropoutsSensor(tracker, entry),
            ]
        )

    for entity_id in tracker.tracked_entities():
        if tracker.should_track(entity_id):
            _add(entity_id)

    _async_remove_orphans(hass, entry, tracker, keys)

    # Lights paired later are picked up the moment the tracker notices them --
    # no reconfiguration, no restart.
    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_NEW_ENTITY, _add)
    )


def _enabled_keys(entry: ConfigEntry) -> tuple[str, ...]:
    """Which per-light sensors this entry exposes.

    An absent option means every sensor, so installs that predate the setting
    keep all of theirs. The result follows SENSOR_KEYS order and drops anything
    unrecognised, so a stale key left in options cannot break setup.
    """
    configured = entry.options.get(CONF_SENSORS)
    if configured is None:
        return tuple(DEFAULT_SENSORS)
    chosen = set(configured)
    return tuple(key for key in SENSOR_KEYS if key in chosen)


def _split_unique_id(unique_id: str) -> tuple[str, str] | None:
    """Split a per-light unique_id into its source registry id and sensor key."""
    for key in SENSOR_KEYS:
        suffix = f"_{key}"
        if unique_id.endswith(suffix):
            return unique_id[: -len(suffix)], key
    return None


@callback
def _async_remove_orphans(
    hass: HomeAssistant,
    entry: ConfigEntry,
    tracker: LightLifetimeTracker,
    keys: tuple[str, ...],
) -> None:
    """Drop entities the current options no longer ask for.

    Without this, narrowing the selection in the options flow -- fewer lights,
    or fewer sensors per light -- would leave stale entities behind that never
    update again.
    """
    registry = er.async_get(hass)
    # unique_id is "<source registry id>_<key>", so map registry ids back to
    # entity_ids once rather than scanning per sensor.
    by_registry_id = {e.id: e.entity_id for e in registry.entities.values()}
    summary_prefix = f"{entry.entry_id}_summary_"
    summaries = entry.options.get(CONF_SUMMARY_SENSORS, DEFAULT_SUMMARY_SENSORS)

    for sensor in er.async_entries_for_config_entry(registry, entry.entry_id):
        # Fleet aggregates belong to the entry itself, not to any light, so
        # they are matched by prefix before the per-light parsing below --
        # "<entry_id>_summary_on_hours" also ends in a sensor key.
        if sensor.unique_id.startswith(summary_prefix):
            if not summaries:
                registry.async_remove(sensor.entity_id)
                _LOGGER.debug("Removed %s; summary sensors are off", sensor.entity_id)
            continue

        split = _split_unique_id(sensor.unique_id)
        if split is None:
            continue
        source_id, key = split
        source_entity_id = by_registry_id.get(source_id)
        if source_entity_id is None:
            # A registry id that no longer resolves means the bulb was deleted,
            # so nothing will ever update this sensor again. The registry is
            # fully loaded before any config entry is set up, so an id shaped
            # like one and missing from it really is gone.
            #
            # `_stable_id` falls back to the entity_id for lights that were
            # never registered, and those platforms may not have set up yet at
            # this point, so an entity_id-shaped source is left alone rather
            # than risk deleting a live sensor over a startup race.
            if "." in source_id:
                continue
            registry.async_remove(sensor.entity_id)
            _LOGGER.debug(
                "Removed %s; its source light is no longer registered",
                sensor.entity_id,
            )
            continue
        if key not in keys:
            registry.async_remove(sensor.entity_id)
            _LOGGER.debug("Removed %s; %s is not exposed", sensor.entity_id, key)
        elif not tracker.should_track(source_entity_id):
            registry.async_remove(sensor.entity_id)
            _LOGGER.debug(
                "Removed %s; %s is no longer tracked",
                sensor.entity_id,
                source_entity_id,
            )


class LightLifetimeSensorBase(SensorEntity):
    """Shared plumbing: identity, device linkage and update subscriptions."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _key: str = ""

    def __init__(self, tracker: LightLifetimeTracker, source_entity_id: str) -> None:
        self._tracker = tracker
        self._source_entity_id = source_entity_id
        self._attr_translation_key = self._key
        self._attr_unique_id = f"{self._stable_id(tracker, source_entity_id)}_{self._key}"
        # Attach to the bulb's own device. Assigning `device_entry` directly is
        # the supported way to join a device owned by another config entry;
        # passing DeviceInfo with its identifiers is deprecated in 2026.x and
        # removed in 2027.8, and silently creates a duplicate device instead.
        self.device_entry = async_entity_id_to_device(tracker.hass, source_entity_id)
        if self.device_entry is None:
            # Without a device to hang off, fall back to a readable standalone
            # name derived from the source entity.
            self._attr_has_entity_name = False
            friendly = source_entity_id.split(".", 1)[1].replace("_", " ").title()
            self._attr_name = f"{friendly} {self._key.replace('_', ' ')}"

    @staticmethod
    def _stable_id(tracker: LightLifetimeTracker, source_entity_id: str) -> str:
        """Prefer the registry's immutable id so renames don't orphan entities."""
        entry = er.async_get(tracker.hass).async_get(source_entity_id)
        return entry.id if entry is not None else source_entity_id

    async def async_added_to_hass(self) -> None:
        @callback
        def _updated(entity_id: str) -> None:
            if entity_id == self._source_entity_id:
                self.async_write_ha_state()

        @callback
        def _refresh() -> None:
            self.async_write_ha_state()

        @callback
        def _renamed(old_id: str, new_id: str) -> None:
            # The tracker moves the ledger key on a rename; follow it, or this
            # sensor keeps asking about an entity_id that no longer has a
            # record and reports 0 from here on.
            if old_id == self._source_entity_id:
                self._source_entity_id = new_id
                self.async_write_ha_state()

        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_UPDATED, _updated)
        )
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_REFRESH, _refresh)
        )
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_RENAMED, _renamed)
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"source_entity_id": self._source_entity_id}


class LightOnHoursSensor(LightLifetimeSensorBase):
    """Lifetime hours the light has spent switched on."""

    _key = "on_hours"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.HOURS
    # total_increasing enrols the value in long-term statistics, which the
    # recorder never purges, and tolerates a reset back to zero on bulb swap.
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_suggested_display_precision = 1
    _attr_icon = "mdi:lightbulb-on-outline"

    @property
    def native_value(self) -> float:
        return round(self._tracker.on_seconds(self._source_entity_id) / 3600.0, 4)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "source_entity_id": self._source_entity_id,
            "currently_on": self._tracker.is_on(self._source_entity_id),
        }


class LightDropoutsSensor(LightLifetimeSensorBase):
    """How many times the light has dropped to unavailable."""

    _key = "dropouts"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = "dropouts"
    _attr_icon = "mdi:lan-disconnect"

    @property
    def native_value(self) -> int:
        return self._tracker.dropouts(self._source_entity_id)


class LightTurnOnCountSensor(LightLifetimeSensorBase):
    """How many times the light has been switched on."""

    _key = "turn_on_count"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = "times"
    _attr_icon = "mdi:toggle-switch-variant"

    @property
    def native_value(self) -> int:
        return self._tracker.turn_on_count(self._source_entity_id)


class LightTurnOffCountSensor(LightLifetimeSensorBase):
    """How many times the light has been switched off."""

    _key = "turn_off_count"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = "times"
    _attr_icon = "mdi:toggle-switch-variant-off"

    @property
    def native_value(self) -> int:
        return self._tracker.turn_off_count(self._source_entity_id)


class LightFirstSeenSensor(LightLifetimeSensorBase):
    """When the light was first added to Home Assistant, if that is knowable."""

    _key = "first_seen"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:calendar-plus"

    @property
    def native_value(self) -> datetime | None:
        return self._tracker.first_seen(self._source_entity_id)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        source = self._tracker.first_seen_source(self._source_entity_id)
        tracked_since = self._tracker.tracked_since(self._source_entity_id)
        return {
            "source_entity_id": self._source_entity_id,
            ATTR_FIRST_SEEN_SOURCE: source,
            ATTR_TRACKED_SINCE: tracked_since.isoformat() if tracked_since else None,
            # True when the registry only carries the epoch sentinel, so all we
            # can honestly say is "already present when tracking began".
            "is_floor": source == SOURCE_UNKNOWN,
        }


class LightAgeSensor(LightLifetimeSensorBase):
    """Hours since the light was first connected."""

    _key = "age"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.HOURS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0
    _attr_icon = "mdi:clock-outline"

    @property
    def native_value(self) -> float | None:
        first_seen = self._tracker.first_seen(self._source_entity_id)
        if first_seen is None:
            return None
        return round((dt_util.utcnow() - first_seen).total_seconds() / 3600.0, 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "source_entity_id": self._source_entity_id,
            "is_floor": self._tracker.first_seen_source(self._source_entity_id)
            == SOURCE_UNKNOWN,
        }


# Keyed by sensor key so the enabled set drives construction directly.
SENSOR_TYPES: dict[str, type[LightLifetimeSensorBase]] = {
    "on_hours": LightOnHoursSensor,
    "dropouts": LightDropoutsSensor,
    "turn_on_count": LightTurnOnCountSensor,
    "turn_off_count": LightTurnOffCountSensor,
    "first_seen": LightFirstSeenSensor,
    "age": LightAgeSensor,
}


class SummarySensorBase(SensorEntity):
    """A whole-collection total, not tied to any single light."""

    _attr_should_poll = False
    _key: str = ""

    def __init__(self, tracker: LightLifetimeTracker, entry: ConfigEntry) -> None:
        self._tracker = tracker
        self._attr_unique_id = f"{entry.entry_id}_summary_{self._key}"

    async def async_added_to_hass(self) -> None:
        @callback
        def _refresh(*_args: Any) -> None:
            self.async_write_ha_state()

        # Any per-light change moves the aggregate, so listen to both signals.
        for signal in (SIGNAL_UPDATED, SIGNAL_REFRESH, SIGNAL_NEW_ENTITY):
            self.async_on_remove(
                async_dispatcher_connect(self.hass, signal, _refresh)
            )


class TotalOnHoursSensor(SummarySensorBase):
    """Combined on-hours across every tracked light."""

    _key = "on_hours"
    _attr_name = "Lights total hours"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.HOURS
    # A cumulative quantity that can fall: the total is recomputed over
    # whatever is tracked right now, so excluding, resetting or removing a
    # light drops it. total_increasing would read each of those as a counter
    # reset and carry the old total forward, inventing hours the fleet never
    # ran; TOTAL keeps the sum statistic and handles the decrease honestly.
    # Per-light counters stay total_increasing, where a drop really does mean
    # a bulb was replaced.
    _attr_state_class = SensorStateClass.TOTAL
    _attr_suggested_display_precision = 0
    _attr_icon = "mdi:lightbulb-group-outline"

    @property
    def native_value(self) -> float:
        return round(self._tracker.total_on_hours(), 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        count = self._tracker.lights_tracked()
        average = self._tracker.total_on_hours() / count if count else 0.0
        return {"average_hours_per_light": round(average, 2)}


class LightsTrackedSensor(SummarySensorBase):
    """How many lights are being tracked."""

    _key = "tracked"
    _attr_name = "Lights tracked"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "lights"
    _attr_icon = "mdi:lightbulb-multiple-outline"

    @property
    def native_value(self) -> int:
        return self._tracker.lights_tracked()


class LightsOnSensor(SummarySensorBase):
    """How many tracked lights are on right now."""

    _key = "lit"
    _attr_name = "Lights on"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "lights"
    _attr_icon = "mdi:lightbulb-on-outline"

    @property
    def native_value(self) -> int:
        return self._tracker.lights_on()


class TotalDropoutsSensor(SummarySensorBase):
    """Combined dropout count across every tracked light."""

    _key = "dropouts"
    _attr_name = "Lights total dropouts"
    # Cumulative and able to fall, for the same reason as the total above.
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = "dropouts"
    _attr_icon = "mdi:lan-disconnect"

    @property
    def native_value(self) -> int:
        return self._tracker.total_dropouts()
