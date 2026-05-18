"""Diagnostics support for the TFI Live integration."""

from __future__ import annotations

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
        {
            k: v
            for k, v in sensor_cfg.items()
            if k != CONF_API_KEY
        }
        for sensor_cfg in entry.data.get(CONF_SENSORS, [])
    ]

    return {
        "config_entry": config_data,
        "coordinator": coordinator_state,
        "sensors": sensors,
    }
