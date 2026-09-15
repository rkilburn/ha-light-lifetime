"""Config flow for Light Lifetime."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    selector,
)

from .const import (
    CONF_COUNT_DOWNTIME,
    CONF_EXCLUDE_AGGREGATES,
    CONF_EXCLUDED_ENTITIES,
    CONF_INCLUDED_ENTITIES,
    CONF_MANUFACTURERS,
    CONF_MODE,
    DEFAULT_COUNT_DOWNTIME,
    DEFAULT_EXCLUDE_AGGREGATES,
    DEFAULT_MODE,
    DOMAIN,
    MODE_ALL,
    MODE_SELECTED,
)

TITLE = "Light Lifetime"


def _manufacturer_options(hass: HomeAssistant) -> list[str]:
    """Distinct manufacturers across the light devices on this instance.

    Built from the registries rather than hardcoded, so the dropdown offers
    exactly the brands actually present -- and stays correct as hardware changes.
    """
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    found: set[str] = set()
    for entry in ent_reg.entities.values():
        if entry.domain != "light" or not entry.device_id:
            continue
        device = dev_reg.async_get(entry.device_id)
        if device and device.manufacturer:
            found.add(device.manufacturer)
    return sorted(found)


def _schema(hass: HomeAssistant, defaults: dict[str, Any]) -> vol.Schema:
    """Build the setup/options form.

    Two ways to choose what gets tracked: "all" (opt out -- track every light
    except the exclusions, and pick up new lights automatically) or "selected"
    (opt in -- track only the lights listed, and ignore everything else).
    """
    return vol.Schema(
        {
            vol.Required(
                CONF_MODE, default=defaults.get(CONF_MODE, DEFAULT_MODE)
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[MODE_ALL, MODE_SELECTED],
                    translation_key=CONF_MODE,
                    mode=selector.SelectSelectorMode.LIST,
                )
            ),
            vol.Optional(
                CONF_MANUFACTURERS,
                default=defaults.get(CONF_MANUFACTURERS, []),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=_manufacturer_options(hass),
                    multiple=True,
                    custom_value=True,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                    sort=True,
                )
            ),
            vol.Optional(
                CONF_INCLUDED_ENTITIES,
                default=defaults.get(CONF_INCLUDED_ENTITIES, []),
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="light", multiple=True)
            ),
            vol.Optional(
                CONF_EXCLUDED_ENTITIES,
                default=defaults.get(CONF_EXCLUDED_ENTITIES, []),
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="light", multiple=True)
            ),
            vol.Optional(
                CONF_EXCLUDE_AGGREGATES,
                default=defaults.get(
                    CONF_EXCLUDE_AGGREGATES, DEFAULT_EXCLUDE_AGGREGATES
                ),
            ): selector.BooleanSelector(),
            vol.Optional(
                CONF_COUNT_DOWNTIME,
                default=defaults.get(CONF_COUNT_DOWNTIME, DEFAULT_COUNT_DOWNTIME),
            ): selector.BooleanSelector(),
        }
    )


class LightLifetimeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Single-instance config flow; the integration discovers lights itself."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        if user_input is not None:
            return self.async_create_entry(title=TITLE, data={}, options=user_input)

        return self.async_show_form(
            step_id="user", data_schema=_schema(self.hass, {})
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return LightLifetimeOptionsFlow()


class LightLifetimeOptionsFlow(OptionsFlow):
    """Allow the exclusion and downtime settings to be changed later."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=_schema(self.hass, dict(self.config_entry.options)),
        )
