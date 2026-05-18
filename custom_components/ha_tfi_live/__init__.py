"""TFI Live Home Assistant integration."""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_STATIC_GTFS_URL
from .coordinator import TfiLiveCoordinator
from .static_gtfs import StaticGtfsCache

_logger = logging.getLogger(__name__)

type TfiLiveConfigEntry = ConfigEntry[TfiLiveCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: TfiLiveConfigEntry) -> bool:
    """Set up TFI Live from a config entry.

    Creates the static GTFS cache and coordinator, stores the coordinator on
    ``entry.runtime_data``, and forwards setup to the sensor platform.  Static
    GTFS load failures are logged and swallowed — the integration continues
    without static schedule data rather than blocking setup entirely (AC 10).

    Args:
        hass: The Home Assistant instance.
        entry: The config entry being set up.

    Returns:
        True when setup succeeds.

    Raises:
        ConfigEntryAuthFailed: Propagated from the first coordinator refresh
            when the API key is invalid.
        ConfigEntryNotReady: Propagated from the first coordinator refresh when
            the feed is temporarily unavailable.
    """
    cache = StaticGtfsCache(
        static_gtfs_url=entry.data[CONF_STATIC_GTFS_URL],
        session=async_get_clientsession(hass),
    )

    try:
        await cache.async_load()
    except Exception:  # noqa: BLE001
        _logger.warning(
            "Static GTFS load failed -- route names will be unavailable until the"
            " next successful load"
        )

    coordinator = TfiLiveCoordinator(hass, entry, cache)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, [Platform.SENSOR])

    return True


async def async_unload_entry(hass: HomeAssistant, entry: TfiLiveConfigEntry) -> bool:
    """Unload a TFI Live config entry.

    Unloads all forwarded platforms.  The coordinator stored on
    ``entry.runtime_data`` is cleaned up automatically by Home Assistant.

    Args:
        hass: The Home Assistant instance.
        entry: The config entry being unloaded.

    Returns:
        True when all platforms unloaded successfully, False otherwise.
    """
    return await hass.config_entries.async_unload_platforms(entry, [Platform.SENSOR])
