"""Render the README's Markdown dashboard card against HA's template engine."""

from __future__ import annotations

import re
from pathlib import Path

from homeassistant.core import HomeAssistant
from homeassistant.helpers.template import Template

README = Path(__file__).resolve().parent.parent / "README.md"


def _extract_card_template() -> str:
    """Pull the Jinja body out of the markdown card block in the README."""
    block = re.search(r"```yaml\ntype: markdown\ncontent: \|\n(.*?)```", README.read_text(), re.S)
    assert block, "markdown card block not found in README"
    body = block.group(1)
    return "\n".join(line[2:] if line.startswith("  ") else line for line in body.splitlines())


async def test_dashboard_card_renders(hass: HomeAssistant) -> None:
    """The documented card must actually produce a sorted table."""
    hass.states.async_set(
        "sensor.kitchen_fl_on_hours", "9.5", {"friendly_name": "Kitchen FL On hours"}
    )
    hass.states.async_set("sensor.kitchen_fl_dropouts", "2")
    hass.states.async_set("sensor.kitchen_fl_age", "480")

    hass.states.async_set(
        "sensor.hallway_on_hours", "102.25", {"friendly_name": "Hallway On hours"}
    )
    hass.states.async_set("sensor.hallway_dropouts", "11")
    hass.states.async_set("sensor.hallway_age", "unknown")
    await hass.async_block_till_done()

    rendered = Template(_extract_card_template(), hass).async_render(parse_result=False)

    lines = [l for l in rendered.splitlines() if l.strip().startswith("|")]
    data_rows = [l for l in lines if "---" not in l and "Bulb" not in l]

    assert len(data_rows) == 2, f"expected 2 rows, got {data_rows}"
    # 102.25 must sort above 9.5 -- a string sort would invert these.
    assert "Hallway" in data_rows[0], f"numeric sort failed: {data_rows}"
    assert "Kitchen FL" in data_rows[1]
    # Known age renders as whole days; unknown age renders as '?'.
    assert "| 20 |" in data_rows[1], data_rows[1]
    assert "| ? |" in data_rows[0], data_rows[0]
