"""Tests for opt-in (selected) vs opt-out (all) tracking modes."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.light_lifetime.const import (
    CONF_EXCLUDED_ENTITIES,
    CONF_INCLUDED_ENTITIES,
    CONF_MODE,
    DOMAIN,
    MODE_ALL,
    MODE_SELECTED,
)


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
    before = len(hass.states.async_all("sensor"))

    later = _bulb(hass, "new_bulb", "b9")
    hass.states.async_set(later, "on")
    await hass.async_block_till_done()

    assert len(hass.states.async_all("sensor")) == before
    assert later not in tracker.tracked_entities()


async def test_opt_out_mode_still_auto_discovers(hass: HomeAssistant) -> None:
    a = _bulb(hass, "kitchen_fl", "b1")
    hass.states.async_set(a, "off")
    await hass.async_block_till_done()
    await _setup(hass, {CONF_MODE: MODE_ALL})
    before = len(hass.states.async_all("sensor"))

    later = _bulb(hass, "new_bulb", "b9")
    hass.states.async_set(later, "on")
    await hass.async_block_till_done()

    assert len(hass.states.async_all("sensor")) == before + 4


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
    assert len(hass.states.async_all("sensor")) == 8  # 4 per light

    hass.config_entries.async_update_entry(
        entry, options={CONF_MODE: MODE_ALL, CONF_EXCLUDED_ENTITIES: [b]}
    )
    await hass.async_block_till_done()

    remaining = hass.states.async_all("sensor")
    assert len(remaining) == 4, [s.entity_id for s in remaining]
