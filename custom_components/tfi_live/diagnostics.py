"""Diagnostics support for the TFI Live integration."""

from __future__ import annotations

from datetime import date
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_API_KEY, CONF_SENSORS


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics data for a TFI Live config entry.

    Redacts the API key. Includes feed URLs, coordinator state, and
    per-sensor configuration.

    Args:
        hass: The Home Assistant instance.
        entry: The config entry to report diagnostics for.

    Returns:
        A dict safe for serialisation and download by the user.
    """
    coordinator = entry.runtime_data

    config_data = dict(entry.data)
    config_data[CONF_API_KEY] = "**REDACTED**"

    coordinator_state: dict[str, Any] = {
        "last_successful_fetch": (
            coordinator._last_successful_fetch.isoformat()
            if coordinator._last_successful_fetch is not None
            else None
        ),
        "last_update_success": coordinator.last_update_success,
        "entity_count": (
            len(coordinator.data.get("entities", []))
            if coordinator.data is not None
            else None
        ),
    }

    sensors = [
        {k: v for k, v in sensor_cfg.items() if k != CONF_API_KEY}
        for sensor_cfg in entry.data.get(CONF_SENSORS, [])
    ]

    # TEMPORARY (#119 follow-up): dump raw RT stop_time_updates and the
    # static-schedule trip_id set for each configured stop, to compare
    # trip_id/stop_id shapes between the RT feed and the static cache.
    # Revert once diagnosed.
    debug_stop_dumps: list[dict[str, Any]] = []
    if coordinator.data is not None:
        entities = coordinator.data.get("entities", [])
        stop_ids = {s["stop_id"] for s in entry.data.get(CONF_SENSORS, [])}
        for stop_id in stop_ids:
            rt_matches = [
                {
                    "trip_id": entity["trip_id"],
                    "route_id": entity["route_id"],
                    "direction_id": entity["direction_id"],
                    "stop_time_update": stu,
                }
                for entity in entities
                for stu in entity.get("stop_time_updates", [])
                if stu["stop_id"] == stop_id
            ]
            static_departures = coordinator.cache.get_scheduled_departures(
                stop_id, None, None, None, date.today()
            )
            debug_stop_dumps.append(
                {
                    "stop_id": stop_id,
                    "rt_matches": rt_matches,
                    "static_trip_ids": [d[0] for d in static_departures],
                }
            )

    return {
        "config_entry": config_data,
        "coordinator": coordinator_state,
        "sensors": sensors,
        "debug_stop_dumps": debug_stop_dumps,
    }
