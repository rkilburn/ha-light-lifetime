# Light Lifetime

Track **lifetime on-hours**, **dropout counts** and **age** for every light in Home Assistant.

Lights are discovered automatically — including ones you pair later. There is no per-bulb
configuration, no YAML to regenerate, and no dependency on the recorder.

## Why this exists

Home Assistant has no built-in way to answer "how many hours has this bulb actually been on?"

- [`history_stats`](https://www.home-assistant.io/integrations/history_stats/) is bounded by
  `purge_keep_days` (10 by default) and needs a config block per entity.
- Helper-based YAML recipes (`input_number` + automations) persist, but can't create entities
  at runtime, so every new bulb means editing a package and reloading.
- Existing trackers are per-entity and window-based (today / week / year), not lifetime.

This integration keeps its own ledger in `.storage`, completely outside the recorder, and
creates sensors dynamically as lights appear.

## Entities

Four sensors per light, attached to the bulb's own device:

| Sensor | Device class | State class | Notes |
| --- | --- | --- | --- |
| `sensor.<light>_on_hours` | `duration` (h) | `total_increasing` | Lifetime hours switched on |
| `sensor.<light>_dropouts` | — | `total_increasing` | Times the light went `unavailable` |
| `sensor.<light>_first_seen` | `timestamp` | — | When first added, if knowable |
| `sensor.<light>_age` | `duration` (h) | `measurement` | Hours since first connected |

`on_hours` and `dropouts` carry `state_class: total_increasing`, so Home Assistant records them
into **long-term statistics** — which are never purged. You get permanent hourly history per
bulb, which an `input_number` can never give you.

## Installation

### HACS (custom repository)

1. HACS → Integrations → ⋮ → Custom repositories
2. Add `https://github.com/rkilburn/ha-light-lifetime`, category **Integration**
3. Install, restart Home Assistant
4. Settings → Devices & Services → Add Integration → **Light Lifetime**

### Manual

Copy `custom_components/light_lifetime` into your Home Assistant `config/custom_components/`
directory and restart.

## Options

Setup asks for the mode first, then shows only the options that apply to it — a config flow
schema is static, so fields cannot be hidden reactively within a single form.

**Two ways to choose what gets tracked**, set at install and changeable later via Configure:

| Mode | Behaviour |
| --- | --- |
| **Track all lights** (default) | Every light is tracked except the ones you exclude. New lights are picked up automatically. |
| **Track only selected lights** | Only the lights you pick are tracked. New lights are *not* added automatically — that is the trade-off of opting in. |

| Option | Default | Meaning |
| --- | --- | --- |
| Which lights to track | Track all | Opt-out or opt-in, as above. |
| Brands to track | all | Multi-select of the manufacturers found on your system. Empty means every brand. Track-all mode only. |
| Lights to track | none | Opt-in mode only: the explicit allow-list. |
| Lights to ignore | none | Track-all mode only: the exclusions. |
| Exclude groups and rooms | on | Skip light entities that aggregate other lights (Hue Rooms and Zones, HA light groups). Leaving these in double-counts every member bulb. |
| Count downtime as on-time | off | When Home Assistant is offline, assume lights that were on stayed on. Off means only observed time counts. |

The brand dropdown is built from the manufacturers actually present on your instance, read
from the device registry, so it lists real values rather than a guessed set. You can also type
a brand that is not listed yet. A light with no manufacturer recorded cannot match a brand
filter, so it is excluded while one is active.

Narrowing the selection removes the now-unused sensors, but their counters are **kept** in the
ledger — re-include a light later and its history is still there.

## Using the values in automations

The point of a numeric entity is that other automations can use it directly:

```yaml
automation:
  - alias: "Warn when a bulb passes 25,000 hours"
    trigger:
      - platform: numeric_state
        entity_id: sensor.kitchen_fl_on_hours
        above: 25000
    action:
      - service: notify.mobile_app
        data:
          message: "Kitchen front-left has passed its rated life."

  - alias: "Flag a flaky radio"
    trigger:
      - platform: numeric_state
        entity_id: sensor.kitchen_fl_dropouts
        above: 50
    action:
      - service: persistent_notification.create
        data:
          message: "Kitchen front-left has dropped out 50+ times; check its mesh position."
```

Template access works too:

```jinja
{{ states('sensor.kitchen_fl_on_hours') | float(0) }}
{{ (states('sensor.kitchen_fl_age') | float(0) / 24) | round(0) }} days old
```

## Services

### `light_lifetime.reset`

Zero a light's counters and set first-seen to now. **Use this when you physically replace a
bulb** in an existing fixture — the entity stays the same, but the hardware is new.

```yaml
service: light_lifetime.reset
data:
  entity_id: light.kitchen_fl
```

### `light_lifetime.set_values`

Seed or correct counters, e.g. from a known install date or an estimate carried in from
another system.

```yaml
service: light_lifetime.set_values
data:
  entity_id: light.kitchen_fl
  on_hours: 1200
  first_seen: "2023-06-01 12:00:00"
```

### `light_lifetime.backfill`

Seed counters from Home Assistant's own recorder history.

```yaml
action: light_lifetime.backfill
data:
  days: 30          # optional; the recorder returns only what it retained
  overwrite: false  # optional; skip lights that already have time
```

Returns a response with `updated`, `skipped`, `total_hours` and a per-entity breakdown.

**This recovers only what the recorder still holds** — `purge_keep_days`, which defaults to
10. It is a partial seed, not a lifetime total, and there is no way around that: state history
older than the purge window is gone, and lights generate no long-term statistics until this
integration creates them. Lights that already have accumulated time are skipped, so running it
twice will not double-count.

## Dashboard

No custom cards required. This sorts every tracked bulb by on-hours:

```yaml
type: entities
title: Bulb lifetime
entities:
  - entity: sensor.kitchen_fl_on_hours
  - entity: sensor.kitchen_fr_on_hours
  - entity: sensor.living_room_lamp_on_hours
```

For a self-maintaining table that picks up new bulbs on its own, use a Markdown card:

```yaml
type: markdown
content: |
  | Bulb | On hours | Dropouts | Age (days) |
  |---|---:|---:|---:|
  {% set ns = namespace(rows=[]) %}
  {%- for s in states.sensor | selectattr('entity_id', 'search', '_on_hours$') -%}
    {%- set base = s.entity_id[:-9] -%}
    {%- set age = states(base ~ '_age') -%}
    {%- set ns.rows = ns.rows + [(
         s.state | float(0),
         s.name | replace(' On hours', ''),
         states(base ~ '_dropouts'),
         (age | float(0) / 24) | round(0) | int if age not in ['unknown', 'unavailable'] else '?'
       )] -%}
  {%- endfor -%}
  {% for hours, name, drops, age in ns.rows | sort(reverse=true) %}
  | {{ name }} | {{ hours | round(1) }} | {{ drops }} | {{ age }} |
  {%- endfor %}
```

## Design notes

**Attribute churn is ignored.** Only changes where the state *value* differs are counted.
On a fleet running adaptive lighting, brightness and colour updates can be 85% of all light
events — on one real instance, 1712 events/day of which only 266 were genuine transitions.

**Downtime is not silently counted.** The tracker writes a heartbeat every five minutes. If
Home Assistant is killed uncleanly with a light on, the open interval is truncated at the last
heartbeat rather than absorbing the entire outage. Opt into `count_downtime` if you would
rather assume lights stayed on.

**First-seen is never fabricated.** Home Assistant backfilled `created_at` into registry
entries that predate the field using the Unix epoch (`1970-01-01`). Bulbs carrying that
sentinel report `unknown` with `is_floor: true` and a `tracked_since` attribute, rather than
being stamped with today's date and presented as fact. Only genuinely known dates are reported.

> **Note:** on-hours accumulate from installation forward. There is no way to reconstruct
> history for bulbs that predate it — the recorder only keeps `purge_keep_days` of state, and
> lights produce no long-term statistics until this integration creates them. Use
> `light_lifetime.set_values` if you have a credible estimate to seed.

**Storage is a single JSON document** at `.storage/light_lifetime.data`, written through
Home Assistant's own `Store` helper — the same mechanism behind the entity and device
registries. That means atomic writes, debounced to at most once every 60 seconds plus a flush
on shutdown, and inclusion in native Home Assistant backups (`.storage` is not on the backup
component's exclusion list). The recorder's long-term statistics act as an independent second
copy of the numbers, so the values are recoverable even if the ledger is lost.

**Renames are followed.** If you change a light's `entity_id`, its counters move with it.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-test.txt
.venv/bin/python -m pytest tests/ -v
```

## License

MIT
