"""TFI Live Home Assistant integration."""

import logging
import urllib.parse

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from nta_gtfs import GtfsRtClient, StaticGtfsClient

from .const import (
    CONF_API_KEY,
    CONF_STATIC_GTFS_URL,
    CONF_TRIP_UPDATE_URL,
    STATIC_GTFS_REFRESH_HOURS,
)
from .coordinator import TfiLiveCoordinator

_logger = logging.getLogger(__name__)

type TfiLiveConfigEntry = ConfigEntry[TfiLiveCoordinator]


def _strip_format_json(url: str) -> str:
    """Remove any ``format=json`` query parameter from a feed URL.

    The NTA GTFS-R endpoint returns JSON when the URL carries
    ``format=json``, but ``nta_gtfs.GtfsRtClient`` can only parse the
    protobuf default (#99). All other query parameters are preserved.

    Args:
        url: The trip update feed URL to normalise.

    Returns:
        The URL with any ``format=json`` parameter removed.
    """
    parts = urllib.parse.urlsplit(url)
    query = [
        (key, value)
        for key, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        if not (key == "format" and value.lower() == "json")
    ]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate a config entry to the current schema version.

    Version 1.2 strips ``format=json`` from the stored trip update URL so
    entries created with the pre-#99 default recover without being re-added.

    Args:
        hass: The Home Assistant instance.
        entry: The config entry to migrate.

    Returns:
        True when migration succeeds, False when the entry was created by a
        newer version of the integration and cannot be migrated.
    """
    if entry.version > 1:
        return False

    if entry.minor_version < 2:
        trip_update_url: str = entry.data.get(CONF_TRIP_UPDATE_URL, "")
        migrated = _strip_format_json(trip_update_url)
        if migrated != trip_update_url:
            _logger.info(
                "Migrating trip update URL from %s to %s",
                trip_update_url,
                migrated,
            )
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_TRIP_UPDATE_URL: migrated},
            minor_version=2,
        )

    return True


async def async_setup_entry(hass: HomeAssistant, entry: TfiLiveConfigEntry) -> bool:
    """Set up TFI Live from a config entry.

    Creates the static GTFS cache and coordinator, stores the coordinator on
    ``entry.runtime_data``, and forwards setup to the sensor platform.  The
    static GTFS archive is downloaded and parsed in a background task
    scheduled by the coordinator's first refresh, so setup completes quickly
    and sensors run on real-time data alone until the schedule data arrives.

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
    session = async_get_clientsession(hass)

    cache = StaticGtfsClient(
        static_gtfs_url=entry.data[CONF_STATIC_GTFS_URL],
        session=session,
        refresh_hours=STATIC_GTFS_REFRESH_HOURS,
    )

    rt_client = GtfsRtClient(
        feed_url=entry.data[CONF_TRIP_UPDATE_URL],
        api_key=entry.data[CONF_API_KEY],
        session=session,
    )

    coordinator = TfiLiveCoordinator(hass, entry, rt_client, cache)
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
