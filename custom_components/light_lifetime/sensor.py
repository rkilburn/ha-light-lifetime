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
    ATTR_TRACKED_SINCE,
    DOMAIN,
    SIGNAL_NEW_ENTITY,
    SIGNAL_REFRESH,
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

    @callback
    def _add(entity_id: str) -> None:
        if entity_id in known:
            return
        known.add(entity_id)
        async_add_entities(
            [
                LightOnHoursSensor(tracker, entity_id),
                LightDropoutsSensor(tracker, entity_id),
                LightFirstSeenSensor(tracker, entity_id),
                LightAgeSensor(tracker, entity_id),
            ]
        )

    for entity_id in tracker.tracked_entities():
        _add(entity_id)

    # Lights paired later are picked up the moment the tracker notices them --
    # no reconfiguration, no restart.
    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_NEW_ENTITY, _add)
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

        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_UPDATED, _updated)
        )
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_REFRESH, _refresh)
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
