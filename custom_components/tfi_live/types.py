"""Shared TypedDicts for the TFI Live integration."""

from typing import TypedDict


class SensorConfig(TypedDict):
    """Configuration dict for a single TFI Live sensor entity.

    Attributes:
        name: Human-readable display name for the sensor.
        stop_id: GTFS stop identifier (e.g. ``"8220DB000833"``).
        route_id: GTFS route identifier (e.g. ``"60-1-b12-1"``).
        direction_id: GTFS direction (0 or 1), or None to match both.
        operator_id: NTA operator code, or None to match all operators.
    """

    name: str
    stop_id: str
    route_id: str
    direction_id: int | None
    operator_id: str | None


class StopTimeUpdate(TypedDict):
    """Parsed stop-time-update entry from a GTFS-RT trip update.

    Attributes:
        stop_id: GTFS stop identifier.
        arrival_delay: Arrival delay in seconds, or None if absent.
        departure_delay: Departure delay in seconds, or None if absent.
        arrival_time: Scheduled arrival unix timestamp, or None if absent.
        departure_time: Scheduled departure unix timestamp, or None if absent.
    """

    stop_id: str
    arrival_delay: int | None
    departure_delay: int | None
    arrival_time: int | None
    departure_time: int | None
