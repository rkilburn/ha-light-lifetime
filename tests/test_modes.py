"""Tests for opt-in (selected) vs opt-out (all) tracking modes."""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.light_lifetime.const import (
    CONF_EXCLUDED_ENTITIES,
    CONF_INCLUDED_ENTITIES,
    CONF_MODE,
    CONF_SUMMARY_SENSORS,
    DOMAIN,
    MODE_ALL,
    MODE_SELECTED,
    SENSOR_KEYS,
)

PER_LIGHT = len(SENSOR_KEYS)


def _bulb(hass: HomeAssistant, object_id: str, unique: str) -> str:
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    hue = MockConfigEntry(domain="hue")
    hue.add_to_hass(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=hue.entry_id,
        identifiers={("hue", unique)},
        manufacturer="Signify Netherlands B.V.",
        model="Hue white spot",
        name=object_id.replace("_", " ").title(),
    )
    return ent_reg.async_get_or_create(
        "light", "hue", unique, suggested_object_id=object_id, device_id=device.id
    ).entity_id


def _per_light(hass: HomeAssistant) -> list:
    """Per-light sensors only; fleet aggregates carry no source_entity_id."""
    return [
        s
        for s in hass.states.async_all("sensor")
        if "source_entity_id" in s.attributes
    ]


async def _setup(hass: HomeAssistant, options: dict):
    entry = MockConfigEntry(domain=DOMAIN, data={}, options=options, unique_id=DOMAIN)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, hass.data[DOMAIN][entry.entry_id]


async def test_opt_out_mode_tracks_everything_but_exclusions(
    hass: HomeAssistant,
) -> None:
    a = _bulb(hass, "kitchen_fl", "b1")
    b = _bulb(hass, "hallway", "b2")
    hass.states.async_set(a, "off")
    hass.states.async_set(b, "off")
    await hass.async_block_till_done()

    _, tracker = await _setup(
        hass, {CONF_MODE: MODE_ALL, CONF_EXCLUDED_ENTITIES: [b]}
    )
    assert tracker.should_track(a) is True
    assert tracker.should_track(b) is False


async def test_opt_in_mode_tracks_only_selected(hass: HomeAssistant) -> None:
    a = _bulb(hass, "kitchen_fl", "b1")
    b = _bulb(hass, "hallway", "b2")
    hass.states.async_set(a, "off")
    hass.states.async_set(b, "off")
    await hass.async_block_till_done()

    _, tracker = await _setup(
        hass, {CONF_MODE: MODE_SELECTED, CONF_INCLUDED_ENTITIES: [a]}
    )
    assert tracker.should_track(a) is True
    assert tracker.should_track(b) is False


async def test_opt_in_mode_ignores_new_lights(hass: HomeAssistant) -> None:
    """Opting in deliberately trades away auto-discovery."""
    a = _bulb(hass, "kitchen_fl", "b1")
    hass.states.async_set(a, "off")
    await hass.async_block_till_done()
    _, tracker = await _setup(
        hass, {CONF_MODE: MODE_SELECTED, CONF_INCLUDED_ENTITIES: [a]}
    )
    before = len(_per_light(hass))

    later = _bulb(hass, "new_bulb", "b9")
    hass.states.async_set(later, "on")
    await hass.async_block_till_done()

    assert len(_per_light(hass)) == before
    assert later not in tracker.tracked_entities()


async def test_opt_out_mode_still_auto_discovers(hass: HomeAssistant) -> None:
    a = _bulb(hass, "kitchen_fl", "b1")
    hass.states.async_set(a, "off")
    await hass.async_block_till_done()
    await _setup(hass, {CONF_MODE: MODE_ALL})
    before = len(_per_light(hass))

    later = _bulb(hass, "new_bulb", "b9")
    hass.states.async_set(later, "on")
    await hass.async_block_till_done()

    assert len(_per_light(hass)) == before + PER_LIGHT


async def test_narrowing_selection_removes_stale_entities(
    hass: HomeAssistant,
) -> None:
    """Excluding a light later must not leave dead sensors behind."""
    a = _bulb(hass, "kitchen_fl", "b1")
    b = _bulb(hass, "hallway", "b2")
    hass.states.async_set(a, "off")
    hass.states.async_set(b, "off")
    await hass.async_block_till_done()

    entry, _ = await _setup(hass, {CONF_MODE: MODE_ALL})
    assert len(_per_light(hass)) == 2 * PER_LIGHT

    hass.config_entries.async_update_entry(
        entry, options={CONF_MODE: MODE_ALL, CONF_EXCLUDED_ENTITIES: [b]}
    )
    await hass.async_block_till_done()

    remaining = _per_light(hass)
    assert len(remaining) == PER_LIGHT, [s.entity_id for s in remaining]


async def test_summary_sensors_created_by_default(hass: HomeAssistant) -> None:
    """The prebuilt dashboard needs these, so they are on unless turned off."""
    bulb = _bulb(hass, "kitchen_fl", "b1")
    hass.states.async_set(bulb, "off")
    await hass.async_block_till_done()
    await _setup(hass, {CONF_MODE: MODE_ALL})

    ids = {s.entity_id for s in hass.states.async_all("sensor")}
    assert "sensor.lights_total_hours" in ids
    assert "sensor.lights_tracked" in ids
    assert "sensor.lights_on" in ids
    assert "sensor.lights_total_dropouts" in ids


async def test_summary_sensors_can_be_disabled(hass: HomeAssistant) -> None:
    bulb = _bulb(hass, "kitchen_fl", "b1")
    hass.states.async_set(bulb, "off")
    await hass.async_block_till_done()
    await _setup(hass, {CONF_MODE: MODE_ALL, CONF_SUMMARY_SENSORS: False})

    ids = {s.entity_id for s in hass.states.async_all("sensor")}
    assert not any(i.startswith("sensor.lights_") for i in ids), ids
    # Per-light sensors are unaffected.
    assert len(_per_light(hass)) == PER_LIGHT


async def test_summary_totals_reflect_tracked_lights(hass: HomeAssistant) -> None:
    a = _bulb(hass, "kitchen_fl", "b1")
    b = _bulb(hass, "hallway", "b2")
    hass.states.async_set(a, "on")
    hass.states.async_set(b, "off")
    await hass.async_block_till_done()
    _, tracker = await _setup(hass, {CONF_MODE: MODE_ALL})

    tracker._data[a]["on_seconds"] = 3600.0
    tracker._data[b]["on_seconds"] = 1800.0
    tracker._data[b]["dropouts"] = 3

    assert tracker.total_on_hours() == pytest.approx(1.5, abs=0.01)
    assert tracker.total_dropouts() == 3
    assert tracker.lights_tracked() == 2
    assert tracker.lights_on() == 1


async def test_summary_totals_ignore_excluded_lights(hass: HomeAssistant) -> None:
    """History is retained for excluded lights but must not inflate totals."""
    a = _bulb(hass, "kitchen_fl", "b1")
    b = _bulb(hass, "hallway", "b2")
    hass.states.async_set(a, "off")
    hass.states.async_set(b, "off")
    await hass.async_block_till_done()
    entry, tracker = await _setup(
        hass, {CONF_MODE: MODE_ALL, CONF_EXCLUDED_ENTITIES: [b]}
    )

    tracker._ensure(b)["on_seconds"] = 7200.0
    tracker._data[a]["on_seconds"] = 3600.0

    assert tracker.lights_tracked() == 1
    assert tracker.total_on_hours() == pytest.approx(1.0, abs=0.01)
