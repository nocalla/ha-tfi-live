"""Constants for the TFI Live integration.

All string keys and numeric tunables used across the integration are
defined here so that every other module imports from a single source of
truth rather than using bare string literals.
"""

from typing import Final

# Integration identity
DOMAIN: Final[str] = "tfi_live"

# Default feed URLs (pre-fill config flow step 1)
# NOTE: no query parameters — the NTA endpoint's default response is the
# protobuf FeedMessage that nta_gtfs.GtfsRtClient expects. A `format=json`
# parameter makes the API return JSON, which the client cannot parse (#99).
DEFAULT_TRIP_UPDATE_URL: Final[str] = (
    "https://api.nationaltransport.ie/gtfsr/v2/TripUpdates"
)
DEFAULT_STATIC_GTFS_URL: Final[str] = (
    "https://www.transportforireland.ie/transitData/Data/GTFS_Realtime.zip"
)

# Config entry keys — used to read/write values in config entry data
CONF_API_KEY: Final[str] = "api_key"
CONF_TRIP_UPDATE_URL: Final[str] = "trip_update_url"
CONF_STATIC_GTFS_URL: Final[str] = "static_gtfs_url"
CONF_STOP_ID: Final[str] = "stop_id"
CONF_ROUTE_ID: Final[str] = "route_id"
CONF_DIRECTION_ID: Final[str] = "direction_id"
CONF_OPERATOR_ID: Final[str] = "operator_id"
CONF_SENSORS: Final[str] = "sensors"

# Sensor extra_state_attributes keys
ATTR_STOP_ID: Final[str] = "stop_id"
ATTR_ROUTE_ID: Final[str] = "route_id"
ATTR_DIRECTION_ID: Final[str] = "direction_id"
ATTR_OPERATOR_ID: Final[str] = "operator_id"
ATTR_DEPARTURES: Final[str] = "departures"
ATTR_LAST_UPDATED: Final[str] = "last_updated"

# Sensor extra_state_attributes keys (continued)
ATTR_NEXT_DEPARTURE_ROUTE_NAME: Final[str] = "next_departure_route_name"

# Departure dict keys — each entry in the departures attribute list
DEP_SCHEDULED_TIME: Final[str] = "scheduled_time"
DEP_REALTIME_TIME: Final[str] = "realtime_time"
DEP_DELAY_MINUTES: Final[str] = "delay_minutes"
DEP_TRIP_ID: Final[str] = "trip_id"
DEP_ROUTE_NAME: Final[str] = "route_name"

# Sentinel value for the route-picker's "All routes at this stop" option.
# Chosen to be distinguishable from any real GTFS route_id.
ALL_ROUTES_SENTINEL: Final[str] = "__all_routes__"

# Numeric tunables
UPDATE_INTERVAL_SECONDS: Final[int] = 60
AVAILABILITY_WINDOW_SECONDS: Final[int] = 180
STATIC_GTFS_REFRESH_HOURS: Final[int] = 24
MAX_DEPARTURES: Final[int] = 3
