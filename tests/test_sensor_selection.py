"""Tests for choosing which sensors each tracked light exposes."""

from __future__ import annotations

from homeassistant.components.sensor import SensorStateClass
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.light_lifetime.const import (
    CONF_MODE,
    CONF_SENSORS,
    CONF_SUMMARY_SENSORS,
    DOMAIN,
    MODE_ALL,
    SENSOR_KEYS,
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


def _keys_in_use(hass: HomeAssistant) -> set[str]:
    """Which sensor keys currently have a per-light entity."""
    found = set()
    for state in hass.states.async_all("sensor"):
        if "source_entity_id" not in state.attributes:
            continue
        for key in SENSOR_KEYS:
            if state.entity_id.endswith(f"_{key}"):
                found.add(key)
    return found


async def _setup(hass: HomeAssistant, options: dict):
    entry = MockConfigEntry(domain=DOMAIN, data={}, options=options, unique_id=DOMAIN)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, hass.data[DOMAIN][entry.entry_id]


async def test_every_sensor_exposed_when_unconfigured(hass: HomeAssistant) -> None:
    """Installs predating the option keep the full set."""
    bulb = _bulb(hass, "kitchen_fl", "b1")
    hass.states.async_set(bulb, "off")
    await hass.async_block_till_done()
    await _setup(hass, {CONF_MODE: MODE_ALL})

    assert _keys_in_use(hass) == set(SENSOR_KEYS)


async def test_only_chosen_sensors_are_created(hass: HomeAssistant) -> None:
    bulb = _bulb(hass, "kitchen_fl", "b1")
    hass.states.async_set(bulb, "off")
    await hass.async_block_till_done()
    await _setup(
        hass,
        {
            CONF_MODE: MODE_ALL,
            CONF_SENSORS: ["on_hours", "turn_on_count"],
            CONF_SUMMARY_SENSORS: False,
        },
    )

    assert _keys_in_use(hass) == {"on_hours", "turn_on_count"}


async def test_empty_selection_creates_no_per_light_sensors(
    hass: HomeAssistant,
) -> None:
    """Somebody who only wants the fleet totals can have just those."""
    bulb = _bulb(hass, "kitchen_fl", "b1")
    hass.states.async_set(bulb, "off")
    await hass.async_block_till_done()
    _, tracker = await _setup(hass, {CONF_MODE: MODE_ALL, CONF_SENSORS: []})

    assert _keys_in_use(hass) == set()
    # Tracking itself is untouched -- only the entities are.
    assert tracker.should_track(bulb) is True
    assert "sensor.lights_total_hours" in {
        s.entity_id for s in hass.states.async_all("sensor")
    }


async def test_unknown_key_in_options_is_ignored(hass: HomeAssistant) -> None:
    """A key left over from a future or older version must not break setup."""
    bulb = _bulb(hass, "kitchen_fl", "b1")
    hass.states.async_set(bulb, "off")
    await hass.async_block_till_done()
    await _setup(
        hass, {CONF_MODE: MODE_ALL, CONF_SENSORS: ["on_hours", "not_a_sensor"]}
    )

    assert _keys_in_use(hass) == {"on_hours"}


async def test_deselecting_a_sensor_removes_it(hass: HomeAssistant) -> None:
    """Narrowing the selection must not leave dead entities behind."""
    bulb = _bulb(hass, "kitchen_fl", "b1")
    hass.states.async_set(bulb, "off")
    await hass.async_block_till_done()
    entry, _ = await _setup(hass, {CONF_MODE: MODE_ALL})
    assert _keys_in_use(hass) == set(SENSOR_KEYS)

    hass.config_entries.async_update_entry(
        entry, options={CONF_MODE: MODE_ALL, CONF_SENSORS: ["on_hours"]}
    )
    await hass.async_block_till_done()

    assert _keys_in_use(hass) == {"on_hours"}
    # Gone from the registry too, not merely absent from the state machine.
    # The fleet total keeps its own dropouts sensor, hence the bulb prefix.
    registered = {
        e.entity_id
        for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    }
    assert "sensor.kitchen_fl_dropouts" not in registered, registered
    assert "sensor.kitchen_fl_on_hours" in registered, registered


async def test_reselecting_a_sensor_restores_its_value(hass: HomeAssistant) -> None:
    """Counters keep running while a sensor is hidden, so nothing is lost."""
    bulb = _bulb(hass, "kitchen_fl", "b1")
    hass.states.async_set(bulb, "off")
    await hass.async_block_till_done()
    entry, tracker = await _setup(
        hass, {CONF_MODE: MODE_ALL, CONF_SENSORS: ["on_hours"]}
    )

    hass.states.async_set(bulb, "on")
    await hass.async_block_till_done()
    hass.states.async_set(bulb, "off")
    await hass.async_block_till_done()
    assert tracker.turn_on_count(bulb) == 1

    hass.config_entries.async_update_entry(
        entry, options={CONF_MODE: MODE_ALL, CONF_SENSORS: ["on_hours", "turn_on_count"]}
    )
    await hass.async_block_till_done()

    turn_on_count = next(
        s
        for s in hass.states.async_all("sensor")
        if s.entity_id.endswith("_turn_on_count")
    )
    assert int(turn_on_count.state) == 1


async def test_disabling_summary_sensors_removes_the_old_ones(
    hass: HomeAssistant,
) -> None:
    """Turning the fleet totals off must clear them, not strand them."""
    bulb = _bulb(hass, "kitchen_fl", "b1")
    hass.states.async_set(bulb, "off")
    await hass.async_block_till_done()
    entry, _ = await _setup(hass, {CONF_MODE: MODE_ALL})
    assert "sensor.lights_total_hours" in {
        s.entity_id for s in hass.states.async_all("sensor")
    }

    hass.config_entries.async_update_entry(
        entry, options={CONF_MODE: MODE_ALL, CONF_SUMMARY_SENSORS: False}
    )
    await hass.async_block_till_done()

    ids = {s.entity_id for s in hass.states.async_all("sensor")}
    assert not any(i.startswith("sensor.lights_") for i in ids), ids


async def test_turn_count_sensors_report_the_ledger(hass: HomeAssistant) -> None:
    """The new sensors carry statistics metadata, not just a number."""
    bulb = _bulb(hass, "kitchen_fl", "b1")
    hass.states.async_set(bulb, "off")
    await hass.async_block_till_done()
    await _setup(hass, {CONF_MODE: MODE_ALL})

    hass.states.async_set(bulb, "on")
    await hass.async_block_till_done()
    hass.states.async_set(bulb, "off")
    await hass.async_block_till_done()

    by_suffix = {
        suffix: next(
            s
            for s in hass.states.async_all("sensor")
            if s.entity_id.endswith(suffix)
        )
        for suffix in ("_turn_on_count", "_turn_off_count")
    }
    assert int(by_suffix["_turn_on_count"].state) == 1
    assert int(by_suffix["_turn_off_count"].state) == 1
    assert by_suffix["_turn_on_count"].attributes["state_class"] == "total_increasing"
    # Pinned: the unit lands in long-term statistics metadata, so changing it
    # after release makes every user migrate their statistics.
    assert by_suffix["_turn_on_count"].attributes["unit_of_measurement"] == "times"
    assert by_suffix["_turn_off_count"].attributes["unit_of_measurement"] == "times"
    assert by_suffix["_turn_on_count"].attributes["source_entity_id"] == bulb


async def test_fleet_totals_are_totals_not_meters(hass: HomeAssistant) -> None:
    """Fleet totals fall legitimately, so they must not be total_increasing.

    They are recomputed over whatever is tracked right now, so excluding,
    resetting or removing a light drops them. total_increasing would read each
    of those as a counter reset and carry the old total forward, inventing
    hours the fleet never ran. TOTAL keeps the sum statistic and takes the
    decrease at face value.
    """
    bulb = _bulb(hass, "kitchen_fl", "a")
    hass.states.async_set(bulb, "off")
    await hass.async_block_till_done()
    await _setup(hass, {})

    for entity_id in ("sensor.lights_total_hours", "sensor.lights_total_dropouts"):
        state = hass.states.get(entity_id)
        assert state is not None, entity_id
        assert state.attributes["state_class"] == SensorStateClass.TOTAL, entity_id


async def test_fleet_counts_stay_measurements(hass: HomeAssistant) -> None:
    """The two that are instantaneous counts, not cumulative, stay as they were."""
    bulb = _bulb(hass, "kitchen_fl", "a")
    hass.states.async_set(bulb, "off")
    await hass.async_block_till_done()
    await _setup(hass, {})

    for entity_id in ("sensor.lights_tracked", "sensor.lights_on"):
        state = hass.states.get(entity_id)
        assert state is not None, entity_id
        assert state.attributes["state_class"] == SensorStateClass.MEASUREMENT, entity_id


async def test_per_light_counters_stay_total_increasing(hass: HomeAssistant) -> None:
    """A per-light drop really does mean a reset, so that one is a meter."""
    source = _bulb(hass, "kitchen_fl", "a")
    hass.states.async_set(source, "off")
    await hass.async_block_till_done()
    await _setup(hass, {})

    registry = er.async_get(hass)
    for key in ("on_hours", "dropouts", "turn_on_count", "turn_off_count"):
        entity_id = registry.async_get_entity_id(
            "sensor", DOMAIN, f"{registry.async_get(source).id}_{key}"
        )
        state = hass.states.get(entity_id)
        assert state.attributes["state_class"] == SensorStateClass.TOTAL_INCREASING, key
