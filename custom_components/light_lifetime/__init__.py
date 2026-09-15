"""The Light Lifetime integration."""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN, SERVICE_RESET, SERVICE_SET_VALUES
from .tracker import LightLifetimeTracker

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

_ENTITY_SELECTOR = vol.All(cv.ensure_list, [cv.entity_id])

RESET_SCHEMA = vol.Schema({vol.Required(ATTR_ENTITY_ID): _ENTITY_SELECTOR})

SET_VALUES_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): _ENTITY_SELECTOR,
        vol.Optional("on_hours"): vol.Coerce(float),
        vol.Optional("dropouts"): vol.Coerce(int),
        vol.Optional("first_seen"): cv.datetime,
    }
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register services once, regardless of how many entries exist."""

    def _trackers() -> list[LightLifetimeTracker]:
        return list(hass.data.get(DOMAIN, {}).values())

    async def _handle_reset(call: ServiceCall) -> None:
        for entity_id in call.data[ATTR_ENTITY_ID]:
            for tracker in _trackers():
                tracker.reset(entity_id)

    async def _handle_set_values(call: ServiceCall) -> None:
        for entity_id in call.data[ATTR_ENTITY_ID]:
            for tracker in _trackers():
                tracker.set_values(
                    entity_id,
                    on_hours=call.data.get("on_hours"),
                    dropouts=call.data.get("dropouts"),
                    first_seen=call.data.get("first_seen"),
                )

    hass.services.async_register(
        DOMAIN, SERVICE_RESET, _handle_reset, schema=RESET_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SET_VALUES, _handle_set_values, schema=SET_VALUES_SCHEMA
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Light Lifetime from a config entry."""
    tracker = LightLifetimeTracker(hass, entry)
    await tracker.async_setup()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = tracker

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        tracker: LightLifetimeTracker = hass.data[DOMAIN].pop(entry.entry_id)
        await tracker.async_unload()
    return unloaded


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when options change."""
    await hass.config_entries.async_reload(entry.entry_id)
