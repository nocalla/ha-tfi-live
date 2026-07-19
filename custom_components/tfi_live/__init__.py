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
    CONF_SENSORS,
    CONF_STATIC_GTFS_URL,
    CONF_STOP_ID,
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

    Version 1.3 moves the sensor list from ``entry.data`` to
    ``entry.options`` (#144), which is now the single source of truth read
    by every sensor consumer (``async_setup_entry``, ``sensor.py``,
    ``coordinator.py``, ``diagnostics.py``). Entries last touched by the
    pre-#144 options flow already have their sensors under ``entry.options``
    (an ``OptionsFlow``'s ``async_create_entry(data=...)`` is stored there by
    HA, even though the old handler called it with entry-data-shaped
    content) — those are left alone. Entries that have never been through
    the options flow still have their sensors under ``entry.data`` only, so
    those are copied across and stripped from ``entry.data``. Without this
    step, every such entry would silently lose all its sensors on upgrade.

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

    if entry.minor_version < 3:
        if CONF_SENSORS in entry.options:
            sensors = entry.options[CONF_SENSORS]
        else:
            sensors = entry.data.get(CONF_SENSORS, [])
            _logger.info(
                "Migrating %d sensor(s) from entry.data to entry.options",
                len(sensors),
            )
        new_data = {k: v for k, v in entry.data.items() if k != CONF_SENSORS}
        hass.config_entries.async_update_entry(
            entry,
            data=new_data,
            options={**entry.options, CONF_SENSORS: sensors},
            minor_version=3,
        )

    return True


async def async_setup_entry(hass: HomeAssistant, entry: TfiLiveConfigEntry) -> bool:
    """Set up TFI Live from a config entry.

    Creates the static GTFS cache and coordinator, stores the coordinator on
    ``entry.runtime_data``, and forwards setup to the sensor platform.  The
    static GTFS archive is downloaded and parsed in a background task
    scheduled only after a successful coordinator refresh, and only once HA
    has finished starting (#100), so setup completes quickly and sensors run
    on real-time data alone until the schedule data arrives.

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

    # Restrict the static GTFS parse to the configured stops — indexing the
    # full nationwide feed is what OOM-killed low-memory hosts (#100).
    stop_ids = {sensor[CONF_STOP_ID] for sensor in entry.options.get(CONF_SENSORS, [])}

    cache = StaticGtfsClient(
        static_gtfs_url=entry.data[CONF_STATIC_GTFS_URL],
        session=session,
        refresh_hours=STATIC_GTFS_REFRESH_HOURS,
        stop_ids=stop_ids,
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
