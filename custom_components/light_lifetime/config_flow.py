"""Config flow for Light Lifetime.

The flow is split across steps rather than shown as one long form: a config
flow schema is static, so fields cannot be hidden reactively. Asking for the
mode first means each following step only offers options that actually apply
to the chosen mode.
"""

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
from homeassistant.data_entry_flow import section
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
    SECTION_BRANDS,
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


def _mode_schema(defaults: dict[str, Any]) -> vol.Schema:
    """Step one: how to choose what gets tracked."""
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
            )
        }
    )


def _all_schema(hass: HomeAssistant, defaults: dict[str, Any]) -> vol.Schema:
    """Options that only make sense when tracking every light."""
    return vol.Schema(
        {
            # The section exists for layout: a multi-select dropdown renders its
            # label inside the "add" chip, leaving the field with no heading.
            # The section supplies the heading and the guidance above the input.
            vol.Required(SECTION_BRANDS): section(
                vol.Schema(
                    {
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
                        )
                    }
                ),
                {"collapsed": False},
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


def _selected_schema(defaults: dict[str, Any]) -> vol.Schema:
    """Options that only make sense when tracking an explicit allow-list."""
    return vol.Schema(
        {
            vol.Optional(
                CONF_INCLUDED_ENTITIES,
                default=defaults.get(CONF_INCLUDED_ENTITIES, []),
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="light", multiple=True)
            ),
            vol.Optional(
                CONF_COUNT_DOWNTIME,
                default=defaults.get(CONF_COUNT_DOWNTIME, DEFAULT_COUNT_DOWNTIME),
            ): selector.BooleanSelector(),
        }
    )


def _flatten(user_input: dict[str, Any]) -> dict[str, Any]:
    """Lift section fields to the top level so stored options stay flat."""
    flat = dict(user_input)
    flat.update(flat.pop(SECTION_BRANDS, None) or {})
    return flat


class _SharedSteps:
    """Mode-dependent steps, shared by the config and options flows."""

    _collected: dict[str, Any]
    _defaults: dict[str, Any]

    def _finish(self, options: dict[str, Any]) -> ConfigFlowResult:
        raise NotImplementedError

    async def async_step_all(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self._finish({**self._collected, **_flatten(user_input)})
        return self.async_show_form(
            step_id="all",
            data_schema=_all_schema(self.hass, self._defaults),
        )

    async def async_step_selected(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self._finish({**self._collected, **_flatten(user_input)})
        return self.async_show_form(
            step_id="selected",
            data_schema=_selected_schema(self._defaults),
        )

    def _route(self, mode: str) -> Any:
        return (
            self.async_step_selected()
            if mode == MODE_SELECTED
            else self.async_step_all()
        )


class LightLifetimeConfigFlow(_SharedSteps, ConfigFlow, domain=DOMAIN):
    """Single-instance config flow; the integration discovers lights itself."""

    VERSION = 1

    def __init__(self) -> None:
        self._collected = {}
        self._defaults = {}

    def _finish(self, options: dict[str, Any]) -> ConfigFlowResult:
        return self.async_create_entry(title=TITLE, data={}, options=options)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        if user_input is not None:
            self._collected = dict(user_input)
            return await self._route(user_input[CONF_MODE])

        return self.async_show_form(step_id="user", data_schema=_mode_schema({}))

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return LightLifetimeOptionsFlow()


class LightLifetimeOptionsFlow(_SharedSteps, OptionsFlow):
    """Same steps, pre-filled from the options already in force."""

    def __init__(self) -> None:
        self._collected = {}
        self._defaults = {}

    def _finish(self, options: dict[str, Any]) -> ConfigFlowResult:
        return self.async_create_entry(title="", data=options)

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        self._defaults = dict(self.config_entry.options)

        if user_input is not None:
            self._collected = dict(user_input)
            return await self._route(user_input[CONF_MODE])

        return self.async_show_form(
            step_id="init", data_schema=_mode_schema(self._defaults)
        )
