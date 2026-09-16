"""Constants for the Light Lifetime integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "light_lifetime"

# Storage
STORAGE_VERSION: Final = 1
STORAGE_KEY: Final = f"{DOMAIN}.data"
# Debounce writes to .storage; the tracker also flushes on shutdown.
SAVE_DELAY: Final = 60
# How often the heartbeat is refreshed. On an unclean shutdown the heartbeat is
# the last moment we know Home Assistant was alive, so an open "on" interval can
# be truncated there instead of silently absorbing the whole outage.
HEARTBEAT_INTERVAL: Final = 300

# Config / options keys
CONF_MODE: Final = "mode"
CONF_INCLUDED_ENTITIES: Final = "included_entities"
# Form-only grouping key. A section renders a heading and description above
# its fields; the value is flattened away before options are stored, so the
# persisted shape stays flat.
SECTION_BRANDS: Final = "brand_filter"
CONF_SUMMARY_SENSORS: Final = "summary_sensors"
# Which per-light sensors to create. Absent means "all of them".
CONF_SENSORS: Final = "sensors"
CONF_MANUFACTURERS: Final = "manufacturers"
CONF_EXCLUDE_AGGREGATES: Final = "exclude_aggregates"
CONF_EXCLUDED_ENTITIES: Final = "excluded_entities"
CONF_COUNT_DOWNTIME: Final = "count_downtime"

# "all": track every light except the exclusions (new lights auto-tracked).
# "selected": track only the chosen lights (new lights are NOT auto-tracked).
MODE_ALL: Final = "all"
MODE_SELECTED: Final = "selected"
DEFAULT_MODE: Final = MODE_ALL

DEFAULT_EXCLUDE_AGGREGATES: Final = True
# On by default: the prebuilt example dashboard depends on these.
DEFAULT_SUMMARY_SENSORS: Final = True
DEFAULT_COUNT_DOWNTIME: Final = False

# Device models that represent a group of bulbs rather than a physical light.
# Hue exposes Rooms and Zones as light entities backed by a device; counting
# them double-counts every member bulb's on-time.
AGGREGATE_MODELS: Final = frozenset({"room", "zone"})
# Integrations whose light entities are purely aggregates of other lights.
AGGREGATE_PLATFORMS: Final = frozenset({"group", "light_group", "switch_as_x"})

# Home Assistant backfilled `created_at` into pre-existing registry entries with
# the Unix epoch. Anything at or below this is a sentinel, not a real date.
EPOCH_SENTINEL_YEAR: Final = 1971

# Record keys
ATTR_ON_SECONDS: Final = "on_seconds"
ATTR_DROPOUTS: Final = "dropouts"
ATTR_TURN_ON_COUNT: Final = "turn_on_count"
ATTR_TURN_OFF_COUNT: Final = "turn_off_count"
ATTR_FIRST_SEEN: Final = "first_seen"
ATTR_FIRST_SEEN_SOURCE: Final = "first_seen_source"
ATTR_TRACKED_SINCE: Final = "tracked_since"
ATTR_ON_SINCE: Final = "on_since"

SOURCE_REGISTRY: Final = "registry"
# A date somebody stated rather than one Home Assistant recorded: the moment a
# bulb was reset, or a first_seen passed to set_values. It is a real date, so
# it is not a floor -- but it did not come from the registry and should not
# claim to have.
SOURCE_MANUAL: Final = "manual"
SOURCE_UNKNOWN: Final = "unknown"

# Sensor suffixes. Order matters only in that lookups must match the whole
# key -- "on_hours" and "first_seen" contain underscores, so a naive
# rsplit("_", 1) on a unique_id silently mis-parses them.
SENSOR_KEYS: Final = (
    "on_hours",
    "dropouts",
    "turn_on_count",
    "turn_off_count",
    "first_seen",
    "age",
)
# Every sensor is offered by default; the options flow narrows the set.
DEFAULT_SENSORS: Final = SENSOR_KEYS

SIGNAL_NEW_ENTITY: Final = f"{DOMAIN}_new_entity"
SIGNAL_UPDATED: Final = f"{DOMAIN}_updated"
SIGNAL_REFRESH: Final = f"{DOMAIN}_refresh"

SERVICE_RESET: Final = "reset"
SERVICE_SET_VALUES: Final = "set_values"
SERVICE_BACKFILL: Final = "backfill"
