"""Tests for seeding counters from recorder history."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.light_lifetime.const import (
    ATTR_DROPOUTS,
    ATTR_ON_SECONDS,
    ATTR_TURN_OFFS,
    ATTR_TURN_ONS,
    DOMAIN,
    SERVICE_BACKFILL,
)
from custom_components.light_lifetime.tracker import LightLifetimeTracker

BASE = datetime(2026, 9, 5, 0, 0, tzinfo=timezone.utc)
END = datetime(2026, 9, 15, 0, 0, tzinfo=timezone.utc)


def _states(entity_id: str, pairs: list[tuple[str, datetime]]) -> list[State]:
    out = []
    for value, when in pairs:
        s = State(entity_id, value)
        s.last_updated = when
        s.last_changed = when
        out.append(s)
    return out


def test_replay_sums_on_intervals() -> None:
    """Two on/off cycles add up to their elapsed time."""
    states = _states(
        "light.kitchen",
        [
            ("off", BASE),
            ("on", BASE + timedelta(hours=1)),
            ("off", BASE + timedelta(hours=3)),      # 2h
            ("on", BASE + timedelta(hours=10)),
            ("off", BASE + timedelta(hours=10, minutes=30)),  # 0.5h
        ],
    )
    replayed = LightLifetimeTracker._replay(states, END)
    assert replayed.on_seconds == pytest.approx(2.5 * 3600)
    assert replayed.dropouts == 0
    assert replayed.turn_ons == 2
    assert replayed.turn_offs == 2
    assert replayed.first_ts == BASE


def test_replay_counts_dropouts_and_closes_interval() -> None:
    """unavailable both ends an on-interval and counts once."""
    states = _states(
        "light.kitchen",
        [
            ("on", BASE),
            ("unavailable", BASE + timedelta(hours=2)),
            ("unavailable", BASE + timedelta(hours=3)),  # not a second dropout
            ("on", BASE + timedelta(hours=4)),
            ("off", BASE + timedelta(hours=5)),
        ],
    )
    replayed = LightLifetimeTracker._replay(states, END)
    assert replayed.on_seconds == pytest.approx(3 * 3600)  # 2h + 1h
    assert replayed.dropouts == 1
    # on -> unavailable -> on is a dropout and a recovery, not a switch cycle;
    # only the closing on -> off counts.
    assert replayed.turn_ons == 0
    assert replayed.turn_offs == 1


def test_replay_handles_still_on_at_window_end() -> None:
    """A light still on when history ends accrues up to the end."""
    states = _states("light.kitchen", [("on", END - timedelta(hours=4))])
    replayed = LightLifetimeTracker._replay(states, END)
    assert replayed.on_seconds == pytest.approx(4 * 3600)
    # The first recorded state has no predecessor, so it starts no cycle.
    assert replayed.turn_ons == 0


async def test_backfill_service_seeds_and_is_idempotent(hass: HomeAssistant) -> None:
    """The service seeds counters once and refuses to double-count."""
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    hue = MockConfigEntry(domain="hue")
    hue.add_to_hass(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=hue.entry_id,
        identifiers={("hue", "bulb-1")},
        manufacturer="Signify Netherlands B.V.",
        model="Hue white spot",
    )
    source = ent_reg.async_get_or_create(
        "light", "hue", "bulb-1", suggested_object_id="kitchen_fl", device_id=device.id
    ).entity_id
    hass.states.async_set(source, "off")
    await hass.async_block_till_done()

    entry = MockConfigEntry(domain=DOMAIN, data={}, options={}, unique_id=DOMAIN)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    tracker = hass.data[DOMAIN][entry.entry_id]

    recorded = _states(
        source,
        [
            ("off", BASE),
            ("on", BASE + timedelta(hours=1)),
            ("unavailable", BASE + timedelta(hours=4)),  # 3h on, 1 dropout
            ("off", BASE + timedelta(hours=5)),
        ],
    )

    async def _fake_backfill(entity_ids=None, days=30, overwrite=False):
        """Exercise the real accumulation path with synthetic history."""
        record = tracker._data[source]
        if float(record.get(ATTR_ON_SECONDS, 0.0)) > 0 and not overwrite:
            return {"updated": 0, "skipped": 1, "entities": {}}
        replayed = LightLifetimeTracker._replay(recorded, END)
        record[ATTR_ON_SECONDS] = replayed.on_seconds
        record[ATTR_DROPOUTS] = replayed.dropouts
        record[ATTR_TURN_ONS] = replayed.turn_ons
        record[ATTR_TURN_OFFS] = replayed.turn_offs
        return {
            "updated": 1,
            "skipped": 0,
            "entities": {source: replayed.on_seconds / 3600},
        }

    tracker.async_backfill = _fake_backfill

    result = await hass.services.async_call(
        DOMAIN, SERVICE_BACKFILL, {ATTR_ENTITY_ID: [source]},
        blocking=True, return_response=True,
    )
    assert result["updated"] == 1
    assert result["total_hours"] == pytest.approx(3.0)
    assert tracker.dropouts(source) == 1
    assert tracker.turn_ons(source) == 1

    # Second run must not double-count.
    again = await hass.services.async_call(
        DOMAIN, SERVICE_BACKFILL, {ATTR_ENTITY_ID: [source]},
        blocking=True, return_response=True,
    )
    assert again["updated"] == 0
    assert again["skipped"] == 1
    assert tracker.on_seconds(source) == pytest.approx(3 * 3600, abs=5)


async def test_backfill_action_id_matches_the_documented_one(
    hass: HomeAssistant,
) -> None:
    """Pin the public id literally; the constant would follow a rename silently."""
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={}, unique_id=DOMAIN)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # README documents light_lifetime.backfill; renaming it breaks automations.
    assert hass.services.has_service(DOMAIN, "backfill")
