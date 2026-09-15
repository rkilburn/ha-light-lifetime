"""Tests for the brand (manufacturer) filter."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.light_lifetime.config_flow import _manufacturer_options
from custom_components.light_lifetime.const import (
    CONF_MANUFACTURERS,
    CONF_MODE,
    DOMAIN,
    MODE_ALL,
    MODE_SELECTED,
)

SIGNIFY = "Signify Netherlands B.V."


def _bulb(hass: HomeAssistant, object_id: str, unique: str, manufacturer: str | None,
          model: str = "Hue white spot") -> str:
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    owner = MockConfigEntry(domain="hue")
    owner.add_to_hass(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=owner.entry_id,
        identifiers={("x", unique)},
        manufacturer=manufacturer,
        model=model,
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


async def test_dropdown_options_come_from_the_registry(hass: HomeAssistant) -> None:
    """Options reflect the brands actually present, sorted and deduplicated."""
    _bulb(hass, "hue_a", "a", SIGNIFY)
    _bulb(hass, "hue_b", "b", SIGNIFY)
    _bulb(hass, "printer", "c", "Bambu Lab", model="P2S")
    _bulb(hass, "esp", "d", "Espressif", model="m5stack-atom")
    _bulb(hass, "nameless", "e", None)
    await hass.async_block_till_done()

    assert _manufacturer_options(hass) == ["Bambu Lab", "Espressif", SIGNIFY]


async def test_filter_limits_to_selected_brand(hass: HomeAssistant) -> None:
    hue = _bulb(hass, "hue_a", "a", SIGNIFY)
    bambu = _bulb(hass, "printer", "c", "Bambu Lab", model="P2S")
    hass.states.async_set(hue, "off")
    hass.states.async_set(bambu, "off")
    await hass.async_block_till_done()

    _, tracker = await _setup(
        hass, {CONF_MODE: MODE_ALL, CONF_MANUFACTURERS: [SIGNIFY]}
    )
    assert tracker.should_track(hue) is True
    assert tracker.should_track(bambu) is False


async def test_multiple_brands_selected(hass: HomeAssistant) -> None:
    hue = _bulb(hass, "hue_a", "a", SIGNIFY)
    bambu = _bulb(hass, "printer", "c", "Bambu Lab", model="P2S")
    esp = _bulb(hass, "esp", "d", "Espressif", model="m5stack-atom")
    for e in (hue, bambu, esp):
        hass.states.async_set(e, "off")
    await hass.async_block_till_done()

    _, tracker = await _setup(
        hass, {CONF_MODE: MODE_ALL, CONF_MANUFACTURERS: [SIGNIFY, "Bambu Lab"]}
    )
    assert tracker.should_track(hue) is True
    assert tracker.should_track(bambu) is True
    assert tracker.should_track(esp) is False


async def test_empty_filter_means_all_brands(hass: HomeAssistant) -> None:
    hue = _bulb(hass, "hue_a", "a", SIGNIFY)
    bambu = _bulb(hass, "printer", "c", "Bambu Lab", model="P2S")
    hass.states.async_set(hue, "off")
    hass.states.async_set(bambu, "off")
    await hass.async_block_till_done()

    _, tracker = await _setup(hass, {CONF_MODE: MODE_ALL, CONF_MANUFACTURERS: []})
    assert tracker.should_track(hue) is True
    assert tracker.should_track(bambu) is True


async def test_brandless_light_excluded_when_filter_active(
    hass: HomeAssistant,
) -> None:
    """A light with no manufacturer cannot match a brand filter."""
    nameless = _bulb(hass, "nameless", "e", None)
    hass.states.async_set(nameless, "off")
    await hass.async_block_till_done()

    _, tracker = await _setup(
        hass, {CONF_MODE: MODE_ALL, CONF_MANUFACTURERS: [SIGNIFY]}
    )
    assert tracker.should_track(nameless) is False


async def test_brand_filter_ignored_in_opt_in_mode(hass: HomeAssistant) -> None:
    """An explicit allow-list wins; the brand filter does not second-guess it."""
    bambu = _bulb(hass, "printer", "c", "Bambu Lab", model="P2S")
    hass.states.async_set(bambu, "off")
    await hass.async_block_till_done()

    _, tracker = await _setup(
        hass,
        {
            CONF_MODE: MODE_SELECTED,
            "included_entities": [bambu],
            CONF_MANUFACTURERS: [SIGNIFY],
        },
    )
    assert tracker.should_track(bambu) is True


async def test_rooms_still_excluded_alongside_brand_filter(
    hass: HomeAssistant,
) -> None:
    """Hue Rooms carry the Signify brand but must still be skipped."""
    room = _bulb(hass, "kitchen_room", "r", SIGNIFY, model="Room")
    bulb = _bulb(hass, "kitchen_fl", "k", SIGNIFY)
    hass.states.async_set(room, "off")
    hass.states.async_set(bulb, "off")
    await hass.async_block_till_done()

    _, tracker = await _setup(
        hass, {CONF_MODE: MODE_ALL, CONF_MANUFACTURERS: [SIGNIFY]}
    )
    assert tracker.should_track(room) is False
    assert tracker.should_track(bulb) is True
