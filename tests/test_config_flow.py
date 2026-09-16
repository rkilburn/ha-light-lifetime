"""Tests for the multi-step config and options flows."""

from __future__ import annotations

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.light_lifetime.const import (
    CONF_COUNT_DOWNTIME,
    CONF_EXCLUDED_ENTITIES,
    CONF_INCLUDED_ENTITIES,
    CONF_MANUFACTURERS,
    CONF_MODE,
    CONF_SENSORS,
    DOMAIN,
    MODE_ALL,
    MODE_SELECTED,
    SECTION_BRANDS,
    SENSOR_KEYS,
)

SIGNIFY = "Signify Netherlands B.V."


def _bulb(hass: HomeAssistant, object_id: str, unique: str, manufacturer: str) -> str:
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    owner = MockConfigEntry(domain="hue")
    owner.add_to_hass(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=owner.entry_id,
        identifiers={("x", unique)},
        manufacturer=manufacturer,
        model="Hue white spot",
    )
    return ent_reg.async_get_or_create(
        "light", "hue", unique, suggested_object_id=object_id, device_id=device.id
    ).entity_id


async def test_all_mode_branch_stores_flat_options(hass: HomeAssistant) -> None:
    """Choosing 'all' leads to the all-mode step; the section is flattened away."""
    bulb = _bulb(hass, "kitchen_fl", "a", SIGNIFY)
    hass.states.async_set(bulb, "off")
    await hass.async_block_till_done()

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MODE: MODE_ALL}
    )
    assert result["step_id"] == "all", "mode 'all' must route to the all-mode step"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            SECTION_BRANDS: {CONF_MANUFACTURERS: [SIGNIFY]},
            CONF_EXCLUDED_ENTITIES: [],
            "exclude_aggregates": True,
            CONF_SENSORS: list(SENSOR_KEYS),
            CONF_COUNT_DOWNTIME: False,
        },
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    options = result["options"]
    # The section key must not survive into stored options.
    assert SECTION_BRANDS not in options
    assert options[CONF_MANUFACTURERS] == [SIGNIFY]
    assert options[CONF_MODE] == MODE_ALL


async def test_selected_mode_branch_skips_all_mode_fields(
    hass: HomeAssistant,
) -> None:
    """Opt-in never asks about brands, exclusions or aggregates."""
    bulb = _bulb(hass, "kitchen_fl", "a", SIGNIFY)
    hass.states.async_set(bulb, "off")
    await hass.async_block_till_done()

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MODE: MODE_SELECTED}
    )
    assert result["step_id"] == "selected"

    schema_keys = {str(k) for k in result["data_schema"].schema}
    assert SECTION_BRANDS not in schema_keys
    assert CONF_EXCLUDED_ENTITIES not in schema_keys
    assert "exclude_aggregates" not in schema_keys
    # Choosing the exposed sensors applies to both modes, so it stays.
    assert CONF_SENSORS in schema_keys

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_INCLUDED_ENTITIES: [bulb],
            CONF_SENSORS: ["on_hours", "turn_ons"],
            CONF_COUNT_DOWNTIME: False,
        },
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["options"][CONF_MODE] == MODE_SELECTED
    assert result["options"][CONF_INCLUDED_ENTITIES] == [bulb]
    assert result["options"][CONF_SENSORS] == ["on_hours", "turn_ons"]


async def test_all_mode_step_offers_brand_section(hass: HomeAssistant) -> None:
    """The brand field is present, inside its section, on the all-mode step."""
    _bulb(hass, "kitchen_fl", "a", SIGNIFY)
    await hass.async_block_till_done()

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MODE: MODE_ALL}
    )
    schema_keys = {str(k) for k in result["data_schema"].schema}
    assert SECTION_BRANDS in schema_keys


async def test_options_flow_prefills_and_updates(hass: HomeAssistant) -> None:
    """Reconfiguring keeps existing values as defaults and writes flat options."""
    bulb = _bulb(hass, "kitchen_fl", "a", SIGNIFY)
    hass.states.async_set(bulb, "off")
    await hass.async_block_till_done()

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        options={CONF_MODE: MODE_ALL, CONF_MANUFACTURERS: [SIGNIFY]},
        unique_id=DOMAIN,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_MODE: MODE_ALL}
    )
    assert result["step_id"] == "all"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            SECTION_BRANDS: {CONF_MANUFACTURERS: []},
            CONF_EXCLUDED_ENTITIES: [bulb],
            "exclude_aggregates": True,
            CONF_SENSORS: ["on_hours"],
            CONF_COUNT_DOWNTIME: True,
        },
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert SECTION_BRANDS not in result["data"]
    assert result["data"][CONF_MANUFACTURERS] == []
    assert result["data"][CONF_EXCLUDED_ENTITIES] == [bulb]
    assert result["data"][CONF_SENSORS] == ["on_hours"]
    assert result["data"][CONF_COUNT_DOWNTIME] is True


async def test_single_instance_only(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={}, unique_id=DOMAIN)
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_sensor_picker_defaults_to_every_sensor(hass: HomeAssistant) -> None:
    """A fresh install offers all of them ticked."""
    _bulb(hass, "kitchen_fl", "a", SIGNIFY)
    await hass.async_block_till_done()

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MODE: MODE_ALL}
    )

    field = next(
        k for k in result["data_schema"].schema if str(k) == CONF_SENSORS
    )
    assert field.default() == list(SENSOR_KEYS)


async def test_sensor_picker_prefills_from_current_options(
    hass: HomeAssistant,
) -> None:
    """Reconfiguring shows what is in force, not the defaults."""
    bulb = _bulb(hass, "kitchen_fl", "a", SIGNIFY)
    hass.states.async_set(bulb, "off")
    await hass.async_block_till_done()

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        options={CONF_MODE: MODE_ALL, CONF_SENSORS: ["on_hours", "turn_offs"]},
        unique_id=DOMAIN,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_MODE: MODE_ALL}
    )

    field = next(
        k for k in result["data_schema"].schema if str(k) == CONF_SENSORS
    )
    assert field.default() == ["on_hours", "turn_offs"]
