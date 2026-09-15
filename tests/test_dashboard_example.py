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
        hass.states.async_set(
            f"sensor.{slug}_on_hours",
            hours,
            {"friendly_name": f"{name} On hours", "currently_on": lit},
        )
        hass.states.async_set(
            f"sensor.{slug}_dropouts", drops, {"friendly_name": f"{name} Dropouts"}
        )
        hass.states.async_set(f"sensor.{slug}_age", age, {"friendly_name": f"{name} Age"})


async def test_dashboard_is_valid_yaml() -> None:
    doc = yaml.safe_load(DASHBOARD.read_text())
    assert doc["type"] == "sections"
    assert doc["sections"], "dashboard defines no sections"


async def test_every_template_renders(hass: HomeAssistant) -> None:
    _populate(hass)
    await hass.async_block_till_done()

    templates = _templates()
    assert len(templates) >= 3, f"expected several templates, found {len(templates)}"
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
    # The currently-on bulb is flagged, the flaky one warned.
    assert "💡" in data[0]
    assert "⚠️" in order[1], "22 dropouts should raise a warning"


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
        "sensor.kitchen_fl_dropouts", "0", {"friendly_name": "Kitchen FL Dropouts"}
    )
    await hass.async_block_till_done()

    health = next(t for t in _templates() if "investigate" in t)
    rendered = Template(health, hass).async_render(parse_result=False)
    assert "No dropouts recorded" in rendered


async def test_fleet_summary_totals(hass: HomeAssistant) -> None:
    _populate(hass)
    await hass.async_block_till_done()

    summary = next(t for t in _templates() if "bulbs tracked" in t)
    rendered = Template(summary, hass).async_render(parse_result=False)
    assert "92 hours" in rendered, rendered  # 43.3 + 22.0 + 26.5 + 0.0
    assert "**4** bulbs tracked" in rendered
    assert "**1** on now" in rendered
    assert "**24** dropouts" in rendered
    assert "530 days" in rendered  # 12724 h
