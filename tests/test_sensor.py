"""End-to-end tests: entity creation, persistence, discovery and services."""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.light_lifetime.const import (
    ATTR_ON_SECONDS,
    DOMAIN,
    SENSOR_KEYS,
    SERVICE_RESET,
    SERVICE_SET_VALUES,
    STORAGE_KEY,
)


async def _setup(hass: HomeAssistant, options: dict | None = None):
    entry = MockConfigEntry(
        domain=DOMAIN, data={}, options=options or {}, unique_id=DOMAIN
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, hass.data[DOMAIN][entry.entry_id]


def _make_bulb(hass: HomeAssistant, object_id: str, unique: str) -> str:
    """Register a realistic Hue bulb and return its entity_id."""
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    hue_entry = MockConfigEntry(domain="hue")
    hue_entry.add_to_hass(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=hue_entry.entry_id,
        identifiers={("hue", unique)},
        manufacturer="Signify Netherlands B.V.",
        model="Hue white spot",
        name=object_id.replace("_", " ").title(),
    )
    entry = ent_reg.async_get_or_create(
        "light", "hue", unique, suggested_object_id=object_id, device_id=device.id
    )
    return entry.entity_id


async def test_creates_every_sensor_per_light(hass: HomeAssistant) -> None:
    entity_id = _make_bulb(hass, "kitchen_fl", "bulb-1")
    hass.states.async_set(entity_id, "off")
    await hass.async_block_till_done()
    await _setup(hass)

    for suffix in SENSOR_KEYS:
        matches = [
            s for s in hass.states.async_all("sensor") if s.entity_id.endswith(suffix)
        ]
        assert matches, f"no sensor created for {suffix}"


async def test_on_hours_is_numeric_and_has_statistics_metadata(
    hass: HomeAssistant,
) -> None:
    """The value must be a plain number other automations can compare against."""
    entity_id = _make_bulb(hass, "kitchen_fl", "bulb-1")
    hass.states.async_set(entity_id, "off")
    await hass.async_block_till_done()
    _, tracker = await _setup(hass)

    tracker._data[entity_id][ATTR_ON_SECONDS] = 7200.0
    async_dispatcher_send_state(hass, entity_id)
    await hass.async_block_till_done()

    state = _find_sensor(hass, "on_hours")
    assert float(state.state) == pytest.approx(2.0)
    assert state.attributes["state_class"] == "total_increasing"
    assert state.attributes["device_class"] == "duration"
    assert state.attributes["unit_of_measurement"] == "h"
    assert state.attributes["source_entity_id"] == entity_id


async def test_sensor_attached_to_bulb_device(hass: HomeAssistant) -> None:
    """Stats should land on the bulb's own device page."""
    entity_id = _make_bulb(hass, "kitchen_fl", "bulb-1")
    hass.states.async_set(entity_id, "off")
    await hass.async_block_till_done()
    await _setup(hass)

    ent_reg = er.async_get(hass)
    source = ent_reg.async_get(entity_id)
    sensor = _find_sensor(hass, "on_hours")
    assert ent_reg.async_get(sensor.entity_id).device_id == source.device_id


async def test_new_light_is_discovered_without_reconfiguration(
    hass: HomeAssistant,
) -> None:
    """The whole point: pair a bulb later, get sensors immediately."""
    first = _make_bulb(hass, "kitchen_fl", "bulb-1")
    hass.states.async_set(first, "off")
    await hass.async_block_till_done()
    await _setup(hass)

    before = len(hass.states.async_all("sensor"))

    later = _make_bulb(hass, "hallway_new", "bulb-2")
    hass.states.async_set(later, "on")
    await hass.async_block_till_done()

    after = len(hass.states.async_all("sensor"))
    assert after == before + len(
        SENSOR_KEYS
    ), "expected a full set of sensors for the newly paired bulb"


async def test_unknown_first_seen_is_not_fabricated(hass: HomeAssistant) -> None:
    """Bulbs carrying the epoch sentinel report unknown, flagged as a floor."""
    from datetime import datetime, timezone

    entity_id = _make_bulb(hass, "old_bulb", "bulb-old")
    ent_reg = er.async_get(hass)
    object.__setattr__(
        ent_reg.async_get(entity_id), "created_at", datetime(1970, 1, 1, tzinfo=timezone.utc)
    )
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get(ent_reg.async_get(entity_id).device_id)
    object.__setattr__(device, "created_at", datetime(1970, 1, 1, tzinfo=timezone.utc))

    hass.states.async_set(entity_id, "off")
    await hass.async_block_till_done()
    await _setup(hass)

    first_seen = _find_sensor(hass, "first_seen")
    assert first_seen.state in ("unknown", "unavailable")
    assert first_seen.attributes["is_floor"] is True
    assert first_seen.attributes["tracked_since"] is not None

    age = _find_sensor(hass, "age")
    assert age.state in ("unknown", "unavailable")


async def test_counters_survive_reload(hass: HomeAssistant, hass_storage) -> None:
    """Values persist through a config entry reload via .storage."""
    entity_id = _make_bulb(hass, "kitchen_fl", "bulb-1")
    hass.states.async_set(entity_id, "off")
    await hass.async_block_till_done()
    entry, tracker = await _setup(hass)

    tracker._data[entity_id][ATTR_ON_SECONDS] = 3600.0 * 42
    await tracker._async_flush()

    assert STORAGE_KEY in hass_storage
    stored = hass_storage[STORAGE_KEY]["data"]["entities"]
    assert stored[entity_id][ATTR_ON_SECONDS] == pytest.approx(3600.0 * 42)

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    reloaded = hass.data[DOMAIN][entry.entry_id]
    assert reloaded.on_seconds(entity_id) == pytest.approx(3600.0 * 42, abs=5)


async def test_reset_service_zeroes_counters(hass: HomeAssistant) -> None:
    """Replacing a bulb needs a clean slate."""
    entity_id = _make_bulb(hass, "kitchen_fl", "bulb-1")
    hass.states.async_set(entity_id, "off")
    await hass.async_block_till_done()
    _, tracker = await _setup(hass)

    tracker._data[entity_id][ATTR_ON_SECONDS] = 99999.0
    tracker._data[entity_id]["dropouts"] = 17

    await hass.services.async_call(
        DOMAIN, SERVICE_RESET, {ATTR_ENTITY_ID: [entity_id]}, blocking=True
    )
    await hass.async_block_till_done()

    assert tracker.on_seconds(entity_id) == pytest.approx(0.0, abs=5)
    assert tracker.dropouts(entity_id) == 0
    assert tracker.first_seen(entity_id) is not None


async def test_set_values_service_seeds_counters(hass: HomeAssistant) -> None:
    entity_id = _make_bulb(hass, "kitchen_fl", "bulb-1")
    hass.states.async_set(entity_id, "off")
    await hass.async_block_till_done()
    _, tracker = await _setup(hass)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_VALUES,
        {ATTR_ENTITY_ID: [entity_id], "on_hours": 500.5, "dropouts": 3},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert tracker.on_seconds(entity_id) == pytest.approx(500.5 * 3600, abs=5)
    assert tracker.dropouts(entity_id) == 3


async def test_full_cycle_through_state_machine(hass: HomeAssistant) -> None:
    """Drive real state changes and confirm on-time lands on the sensor."""
    entity_id = _make_bulb(hass, "kitchen_fl", "bulb-1")
    hass.states.async_set(entity_id, "off")
    await hass.async_block_till_done()
    _, tracker = await _setup(hass)

    hass.states.async_set(entity_id, "on")
    await hass.async_block_till_done()

    # Backdate the open interval by an hour, then switch off.
    tracker._data[entity_id]["on_since"] = (
        dt_util.utcnow() - timedelta(hours=1)
    ).isoformat()
    hass.states.async_set(entity_id, "off")
    await hass.async_block_till_done()

    assert tracker.on_seconds(entity_id) == pytest.approx(3600.0, abs=10)
    assert float(_find_sensor(hass, "on_hours").state) == pytest.approx(1.0, abs=0.01)


# --- helpers ---------------------------------------------------------------


def _find_sensor(hass: HomeAssistant, suffix: str):
    """Find a per-light sensor, ignoring the fleet-level aggregates."""
    for state in hass.states.async_all("sensor"):
        if state.entity_id.endswith(suffix) and "source_entity_id" in state.attributes:
            return state
    raise AssertionError(f"per-light sensor ending in {suffix} not found")


def async_dispatcher_send_state(hass: HomeAssistant, entity_id: str) -> None:
    from homeassistant.helpers.dispatcher import async_dispatcher_send

    from custom_components.light_lifetime.const import SIGNAL_UPDATED

    async_dispatcher_send(hass, SIGNAL_UPDATED, entity_id)


async def test_sensors_follow_an_entity_id_rename(hass: HomeAssistant) -> None:
    """Renaming a light must carry its sensors across, not just the ledger.

    The ledger key moves either way, but each sensor captured its source
    entity_id at construction: without following the rename it asks about an
    id that no longer has a record, reports 0, and stops responding to
    updates.
    """
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    hue = MockConfigEntry(domain="hue")
    hue.add_to_hass(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=hue.entry_id,
        identifiers={("hue", "bulb-rename")},
        manufacturer="Signify Netherlands B.V.",
        model="Hue white spot",
    )
    source = ent_reg.async_get_or_create(
        "light", "hue", "bulb-rename", suggested_object_id="old_name",
        device_id=device.id,
    ).entity_id
    hass.states.async_set(source, "off")
    await hass.async_block_till_done()

    entry = MockConfigEntry(domain=DOMAIN, data={}, options={}, unique_id=DOMAIN)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    tracker = hass.data[DOMAIN][entry.entry_id]

    tracker._data[source][ATTR_ON_SECONDS] = 7200.0
    sensor_id = next(
        e.entity_id
        for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if e.unique_id.endswith("_on_hours") and not e.unique_id.startswith(entry.entry_id)
    )

    ent_reg.async_update_entity(source, new_entity_id="light.new_name")
    await hass.async_block_till_done()

    state = hass.states.get(sensor_id)
    assert float(state.state) == pytest.approx(2.0)
    assert state.attributes["source_entity_id"] == "light.new_name"

    # And it keeps tracking the light under its new id.
    hass.states.async_set("light.new_name", "on")
    await hass.async_block_till_done()
    assert hass.states.get(sensor_id).attributes["currently_on"] is True


async def test_deleted_bulb_does_not_get_ghost_sensors_on_reload(
    hass: HomeAssistant,
) -> None:
    """Deleting a bulb must not duplicate its sensors on the next reload.

    `_stable_id` keys the unique_id on the registry id, so once that is gone a
    rebuild produces a *second* set of sensors keyed on the entity_id instead,
    alongside the originals that can never update again.
    """
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    hue = MockConfigEntry(domain="hue")
    hue.add_to_hass(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=hue.entry_id,
        identifiers={("hue", "bulb-doomed")},
        manufacturer="Signify Netherlands B.V.",
        model="Hue white spot",
    )
    source = ent_reg.async_get_or_create(
        "light", "hue", "bulb-doomed", suggested_object_id="doomed",
        device_id=device.id,
    ).entity_id
    hass.states.async_set(source, "on")
    await hass.async_block_till_done()

    entry = MockConfigEntry(domain=DOMAIN, data={}, options={}, unique_id=DOMAIN)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    per_light = [
        e.entity_id
        for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if not e.unique_id.startswith(entry.entry_id)
    ]
    assert len(per_light) == len(SENSOR_KEYS)

    ent_reg.async_remove(source)
    hass.states.async_remove(source)
    await hass.async_block_till_done()

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    survivors = [
        e.entity_id
        for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if not e.unique_id.startswith(entry.entry_id)
    ]
    assert survivors == []
