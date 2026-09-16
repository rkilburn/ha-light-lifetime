"""Tests for the accumulation logic in LightLifetimeTracker."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.light_lifetime.const import (
    ATTR_DROPOUTS,
    ATTR_ON_SECONDS,
    ATTR_ON_SINCE,
    CONF_COUNT_DOWNTIME,
    DOMAIN,
    SOURCE_REGISTRY,
    SOURCE_UNKNOWN,
)
from custom_components.light_lifetime.tracker import LightLifetimeTracker

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


async def _setup(hass: HomeAssistant, options: dict | None = None):
    entry = MockConfigEntry(
        domain=DOMAIN, data={}, options=options or {}, unique_id=DOMAIN
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, hass.data[DOMAIN][entry.entry_id]


async def test_accumulates_on_time(hass: HomeAssistant) -> None:
    """A full on -> off cycle adds the elapsed time."""
    hass.states.async_set("light.kitchen", "off")
    await hass.async_block_till_done()
    _, tracker = await _setup(hass)

    start = datetime(2026, 9, 15, 10, 0, tzinfo=timezone.utc)
    record = tracker._ensure("light.kitchen")
    tracker._apply_transition(record, "off", "on", start)
    assert record[ATTR_ON_SINCE] is not None

    tracker._apply_transition(record, "on", "off", start + timedelta(hours=2, minutes=30))
    assert record[ATTR_ON_SECONDS] == pytest.approx(9000.0)
    assert record[ATTR_ON_SINCE] is None


async def test_open_interval_counts_live(hass: HomeAssistant) -> None:
    """on_seconds includes the currently-open interval."""
    hass.states.async_set("light.kitchen", "off")
    await hass.async_block_till_done()
    _, tracker = await _setup(hass)

    record = tracker._ensure("light.kitchen")
    from homeassistant.util import dt as dt_util

    record[ATTR_ON_SINCE] = (dt_util.utcnow() - timedelta(hours=1)).isoformat()
    assert tracker.on_seconds("light.kitchen") == pytest.approx(3600.0, abs=5)


async def test_attribute_only_change_ignored(hass: HomeAssistant) -> None:
    """Brightness churn must not create or disturb a record."""
    _, tracker = await _setup(hass)

    hass.states.async_set("light.kitchen", "on", {"brightness": 100})
    await hass.async_block_till_done()
    before = dict(tracker._data.get("light.kitchen", {}))

    for level in (120, 140, 160, 180):
        hass.states.async_set("light.kitchen", "on", {"brightness": level})
    await hass.async_block_till_done()

    after = tracker._data.get("light.kitchen", {})
    assert after.get(ATTR_ON_SINCE) == before.get(ATTR_ON_SINCE)
    assert after.get(ATTR_ON_SECONDS) == before.get(ATTR_ON_SECONDS)


async def test_dropout_counted_and_closes_interval(hass: HomeAssistant) -> None:
    """Going unavailable increments dropouts and stops accruing on-time."""
    _, tracker = await _setup(hass)

    hass.states.async_set("light.kitchen", "on")
    await hass.async_block_till_done()
    hass.states.async_set("light.kitchen", "unavailable")
    await hass.async_block_till_done()

    record = tracker._data["light.kitchen"]
    assert record[ATTR_DROPOUTS] == 1
    assert record[ATTR_ON_SINCE] is None


async def test_startup_unavailable_is_not_a_dropout(hass: HomeAssistant) -> None:
    """old_state is None on restart; that must not inflate the counter."""
    _, tracker = await _setup(hass)

    record = tracker._ensure("light.kitchen")
    tracker._apply_transition(record, None, "unavailable", datetime.now(timezone.utc))
    assert record[ATTR_DROPOUTS] == 0


async def test_repeated_unavailable_counts_once(hass: HomeAssistant) -> None:
    """unavailable -> unavailable is not a new dropout."""
    _, tracker = await _setup(hass)
    record = tracker._ensure("light.kitchen")
    now = datetime.now(timezone.utc)

    tracker._apply_transition(record, "on", "unavailable", now)
    tracker._apply_transition(record, "unavailable", "unavailable", now)
    assert record[ATTR_DROPOUTS] == 1


async def test_epoch_created_at_reported_as_unknown(hass: HomeAssistant) -> None:
    """The 1970 sentinel must not be presented as a real first-seen date."""
    ent_reg = er.async_get(hass)
    entry = ent_reg.async_get_or_create(
        "light", "hue", "unique-old", suggested_object_id="old_bulb"
    )
    # Mimic Home Assistant's backfill of pre-existing registry entries.
    object.__setattr__(entry, "created_at", EPOCH)

    _, tracker = await _setup(hass)
    first_seen, source = tracker._resolve_first_seen(entry.entity_id)
    assert first_seen is None
    assert source == SOURCE_UNKNOWN


async def test_real_created_at_is_used(hass: HomeAssistant) -> None:
    """A genuine creation timestamp is taken from the registry."""
    ent_reg = er.async_get(hass)
    entry = ent_reg.async_get_or_create(
        "light", "hue", "unique-new", suggested_object_id="new_bulb"
    )
    real = datetime(2025, 4, 3, 17, 20, 9, tzinfo=timezone.utc)
    object.__setattr__(entry, "created_at", real)

    _, tracker = await _setup(hass)
    first_seen, source = tracker._resolve_first_seen(entry.entity_id)
    assert source == SOURCE_REGISTRY
    assert first_seen is not None and first_seen.startswith("2025-04-03")


async def test_group_platform_excluded(hass: HomeAssistant) -> None:
    """HA light groups aggregate other lights and must be skipped."""
    ent_reg = er.async_get(hass)
    entry = ent_reg.async_get_or_create(
        "light", "group", "grp-1", suggested_object_id="kitchen_spots"
    )
    _, tracker = await _setup(hass)
    assert tracker._should_track(entry.entity_id) is False


async def test_hue_room_device_excluded(hass: HomeAssistant) -> None:
    """Hue Room devices double-count their member bulbs."""
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    config_entry = MockConfigEntry(domain="hue")
    config_entry.add_to_hass(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={("hue", "room-1")},
        manufacturer="Signify Netherlands B.V.",
        model="Room",
    )
    entry = ent_reg.async_get_or_create(
        "light",
        "hue",
        "room-light-1",
        suggested_object_id="kitchen",
        device_id=device.id,
    )
    _, tracker = await _setup(hass)
    assert tracker._should_track(entry.entity_id) is False


async def test_real_bulb_is_tracked(hass: HomeAssistant) -> None:
    """A normal Hue bulb passes the aggregate filter."""
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    config_entry = MockConfigEntry(domain="hue")
    config_entry.add_to_hass(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={("hue", "bulb-1")},
        manufacturer="Signify Netherlands B.V.",
        model="Hue white spot",
    )
    entry = ent_reg.async_get_or_create(
        "light", "hue", "bulb-1", suggested_object_id="kitchen_fl", device_id=device.id
    )
    _, tracker = await _setup(hass)
    assert tracker._should_track(entry.entity_id) is True


async def test_rename_migrates_counters(hass: HomeAssistant) -> None:
    """Renaming a light must carry its history across."""
    ent_reg = er.async_get(hass)
    entry = ent_reg.async_get_or_create(
        "light", "hue", "bulb-rename", suggested_object_id="old_name"
    )
    _, tracker = await _setup(hass)

    record = tracker._ensure(entry.entity_id)
    record[ATTR_ON_SECONDS] = 1234.0

    ent_reg.async_update_entity(entry.entity_id, new_entity_id="light.new_name")
    await hass.async_block_till_done()

    assert "light.new_name" in tracker._data
    assert tracker._data["light.new_name"][ATTR_ON_SECONDS] == 1234.0
    assert "light.old_name" not in tracker._data


async def test_downtime_truncated_at_heartbeat(hass: HomeAssistant) -> None:
    """An interval left open by a crash is cut at the last known-alive moment."""
    from homeassistant.util import dt as dt_util

    now = dt_util.utcnow()
    tracker = LightLifetimeTracker(
        hass, MockConfigEntry(domain=DOMAIN, options={CONF_COUNT_DOWNTIME: False})
    )
    tracker._data = {
        "light.kitchen": {
            ATTR_ON_SECONDS: 0.0,
            ATTR_DROPOUTS: 0,
            ATTR_ON_SINCE: (now - timedelta(hours=10)).isoformat(),
        }
    }
    # Home Assistant was last seen alive 8 hours ago, so only 2 hours count.
    tracker._heartbeat = now - timedelta(hours=8)
    tracker._close_stale_intervals()

    record = tracker._data["light.kitchen"]
    assert record[ATTR_ON_SECONDS] == pytest.approx(7200.0, abs=5)
    assert record[ATTR_ON_SINCE] is None


async def test_downtime_counted_when_opted_in(hass: HomeAssistant) -> None:
    """With count_downtime the interval is left open across the outage."""
    from homeassistant.util import dt as dt_util

    now = dt_util.utcnow()
    tracker = LightLifetimeTracker(
        hass, MockConfigEntry(domain=DOMAIN, options={CONF_COUNT_DOWNTIME: True})
    )
    opened = (now - timedelta(hours=10)).isoformat()
    tracker._data = {
        "light.kitchen": {ATTR_ON_SECONDS: 0.0, ATTR_DROPOUTS: 0, ATTR_ON_SINCE: opened}
    }
    tracker._heartbeat = now - timedelta(hours=8)
    tracker._close_stale_intervals()

    assert tracker._data["light.kitchen"][ATTR_ON_SINCE] == opened


async def test_switch_cycles_counted_per_direction(hass: HomeAssistant) -> None:
    """Each observed off -> on and on -> off moves its own counter."""
    _, tracker = await _setup(hass)

    hass.states.async_set("light.kitchen", "off")
    await hass.async_block_till_done()
    for _ in range(3):
        hass.states.async_set("light.kitchen", "on")
        await hass.async_block_till_done()
        hass.states.async_set("light.kitchen", "off")
        await hass.async_block_till_done()

    assert tracker.turn_on_count("light.kitchen") == 3
    assert tracker.turn_off_count("light.kitchen") == 3


async def test_attribute_churn_is_not_a_switch_cycle(hass: HomeAssistant) -> None:
    """Brightness updates while on must not inflate the cycle counts."""
    _, tracker = await _setup(hass)

    hass.states.async_set("light.kitchen", "off")
    hass.states.async_set("light.kitchen", "on")
    await hass.async_block_till_done()
    for level in (120, 140, 160, 180):
        hass.states.async_set("light.kitchen", "on", {"brightness": level})
    await hass.async_block_till_done()

    assert tracker.turn_on_count("light.kitchen") == 1
    assert tracker.turn_off_count("light.kitchen") == 0


async def test_dropout_and_recovery_are_not_switch_cycles(
    hass: HomeAssistant,
) -> None:
    """A flaky radio is a dropout, not somebody working the switch."""
    _, tracker = await _setup(hass)
    record = tracker._ensure("light.kitchen")
    now = datetime.now(timezone.utc)

    tracker._apply_transition(record, "on", "unavailable", now)
    tracker._apply_transition(record, "unavailable", "on", now + timedelta(minutes=1))
    tracker._apply_transition(
        record, "on", "unavailable", now + timedelta(minutes=2)
    )
    tracker._apply_transition(
        record, "unavailable", "off", now + timedelta(minutes=3)
    )

    assert tracker.turn_on_count("light.kitchen") == 0
    assert tracker.turn_off_count("light.kitchen") == 0
    assert record[ATTR_DROPOUTS] == 2


async def test_startup_state_is_not_a_switch_cycle(hass: HomeAssistant) -> None:
    """old_state is None when an entity joins the state machine on restart."""
    _, tracker = await _setup(hass)
    record = tracker._ensure("light.kitchen")

    tracker._apply_transition(record, None, "on", datetime.now(timezone.utc))
    assert tracker.turn_on_count("light.kitchen") == 0


async def test_deleted_bulb_stops_counting_but_keeps_its_history(
    hass: HomeAssistant,
) -> None:
    """A bulb removed from Home Assistant must not stay in the fleet totals."""
    ent_reg = er.async_get(hass)
    source = ent_reg.async_get_or_create(
        "light", "hue", "bulb-gone", suggested_object_id="gone"
    ).entity_id
    hass.states.async_set(source, "on")
    await hass.async_block_till_done()
    _, tracker = await _setup(hass)

    tracker._data[source][ATTR_ON_SECONDS] = 7200.0
    assert tracker.lights_tracked() == 1

    ent_reg.async_remove(source)
    hass.states.async_remove(source)
    await hass.async_block_till_done()

    assert tracker.should_track(source) is False
    assert tracker.lights_tracked() == 0
    # The ledger still holds the history, as it does for any untracked light.
    assert tracker._data[source][ATTR_ON_SECONDS] == 7200.0


async def test_yaml_light_without_a_registry_entry_is_still_tracked(
    hass: HomeAssistant,
) -> None:
    """Existence is registry OR state machine; YAML lights have only the latter."""
    hass.states.async_set("light.yaml_only", "on")
    await hass.async_block_till_done()
    _, tracker = await _setup(hass)

    assert er.async_get(hass).async_get("light.yaml_only") is None
    assert tracker.should_track("light.yaml_only") is True


async def test_unknown_entity_id_is_not_tracked(hass: HomeAssistant) -> None:
    """An id that names nothing cannot be a light to track."""
    _, tracker = await _setup(hass)
    assert tracker.should_track("light.never_existed") is False
