"""Render every Jinja template in examples/dashboard.yaml through HA."""

from __future__ import annotations

from pathlib import Path

import yaml
from homeassistant.core import HomeAssistant
from homeassistant.helpers.template import Template

DASHBOARD = Path(__file__).resolve().parent.parent / "examples" / "dashboard.yaml"


def _templates() -> list[str]:
    doc = yaml.safe_load(DASHBOARD.read_text())
    found: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "content" and isinstance(value, str) and "{%" in value:
                    found.append(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(doc)
    return found


def _populate(hass: HomeAssistant) -> None:
    fleet = [
        ("bed_right", "Bed Right", "43.3", "0", "12724", True),
        ("kitchen_fl", "Kitchen FL", "22.0", "2", "unknown", False),
        ("garden_spot_1", "Spot 1", "26.5", "22", "unknown", False),
        ("hallway", "Hallway", "0.0", "0", "unknown", False),
    ]
    for slug, name, hours, drops, age, lit in fleet:
        # source_entity_id is what the cards use to tell per-light sensors
        # apart from the fleet-level aggregates.
        src = {"source_entity_id": f"light.{slug}"}
        hass.states.async_set(
            f"sensor.{slug}_on_hours",
            hours,
            {"friendly_name": f"{name} On hours", "currently_on": lit, **src},
        )
        hass.states.async_set(
            f"sensor.{slug}_dropouts",
            drops,
            {"friendly_name": f"{name} Dropouts", **src},
        )
        hass.states.async_set(
            f"sensor.{slug}_age", age, {"friendly_name": f"{name} Age", **src}
        )


async def test_dashboard_is_a_full_dashboard_config() -> None:
    """The raw configuration editor requires a top-level `views` array."""
    doc = yaml.safe_load(DASHBOARD.read_text())
    assert isinstance(doc.get("views"), list), "top-level 'views' array is required"
    view = doc["views"][0]
    assert view["type"] == "sections"
    assert view["sections"], "view defines no sections"


async def test_every_template_renders(hass: HomeAssistant) -> None:
    _populate(hass)
    await hass.async_block_till_done()

    templates = _templates()
    assert len(templates) >= 2, f"expected several templates, found {len(templates)}"
    for source in templates:
        rendered = Template(source, hass).async_render(parse_result=False)
        assert rendered.strip(), "template rendered empty"
        assert "{%" not in rendered and "{{" not in rendered


async def test_ranking_sorts_numerically_and_marks_podium(
    hass: HomeAssistant,
) -> None:
    _populate(hass)
    await hass.async_block_till_done()

    ranking = next(t for t in _templates() if "🥇" in t)
    rendered = Template(ranking, hass).async_render(parse_result=False)
    rows = [l for l in rendered.splitlines() if l.strip().startswith("|")]
    data = [r for r in rows if "---" not in r and "Bulb" not in r]

    assert "🥇" in data[0] and "Bed Right" in data[0], data[0]
    # 26.5 must outrank 22.0 -- a string sort would put "22.0" above "26.5"? no,
    # but "9.5" vs "22.0" would invert; check the full ordering explicitly.
    order = [d for d in data]
    assert "Spot 1" in order[1], order
    assert "Kitchen FL" in order[2], order
    assert "Hallway" in order[3], order
    # Every row carries a lightbulb icon; the lit one is filled, the rest outlined.
    assert 'icon="mdi:lightbulb"' in data[0], data[0]
    assert "--state-light-active-color" in data[0]
    assert 'icon="mdi:lightbulb-outline"' in order[1], order[1]
    assert "--disabled-text-color" in order[1]
    # The flaky bulb gets an alert icon; a healthy one does not.
    assert 'icon="mdi:alert"' in order[1], "22 dropouts should raise a warning"
    assert 'icon="mdi:alert"' not in data[0]


async def test_health_card_highlights_worst_offender(hass: HomeAssistant) -> None:
    _populate(hass)
    await hass.async_block_till_done()

    health = next(t for t in _templates() if "investigate" in t)
    rendered = Template(health, hass).async_render(parse_result=False)
    assert "Spot 1" in rendered
    assert "🔴 investigate" in rendered
    # 22 of 24 total dropouts
    assert "92%" in rendered, rendered
    assert "Hallway" not in rendered, "zero-dropout bulbs must be omitted"


async def test_health_card_handles_a_clean_fleet(hass: HomeAssistant) -> None:
    hass.states.async_set(
        "sensor.kitchen_fl_dropouts",
        "0",
        {"friendly_name": "Kitchen FL Dropouts", "source_entity_id": "light.kitchen_fl"},
    )
    await hass.async_block_till_done()

    health = next(t for t in _templates() if "investigate" in t)
    rendered = Template(health, hass).async_render(parse_result=False)
    assert "No dropouts recorded" in rendered


async def test_summary_sensors_are_excluded_from_per_light_cards(
    hass: HomeAssistant,
) -> None:
    """Fleet totals end in _on_hours/_dropouts but must not rank as bulbs."""
    _populate(hass)
    hass.states.async_set(
        "sensor.lights_total_hours", "91.8", {"friendly_name": "Lights total hours"}
    )
    hass.states.async_set(
        "sensor.lights_total_dropouts", "24", {"friendly_name": "Lights total dropouts"}
    )
    await hass.async_block_till_done()

    for source in _templates():
        rendered = Template(source, hass).async_render(parse_result=False)
        assert "Lights total hours" not in rendered
        assert "Lights total dropouts" not in rendered


async def test_badges_reference_the_summary_sensors(hass: HomeAssistant) -> None:
    """The prebuilt dashboard depends on the overall light sensors existing."""
    view = yaml.safe_load(DASHBOARD.read_text())["views"][0]
    referenced = {b["entity"] for b in view["badges"]}
    assert referenced == {
        "sensor.lights_total_hours",
        "sensor.lights_on",
        "sensor.lights_tracked",
        "sensor.lights_total_dropouts",
    }, referenced
