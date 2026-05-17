"""DataUpdateCoordinator for the TFI Live integration."""

import json
import logging
from datetime import datetime, timedelta
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import CONF_API_KEY, CONF_TRIP_UPDATE_URL, DOMAIN, UPDATE_INTERVAL_SECONDS
from .static_gtfs import StaticGtfsCache

_logger = logging.getLogger(__name__)


class TfiLiveCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator that fetches and parses the NTA GTFS-RT trip updates feed."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        cache: StaticGtfsCache,
    ) -> None:
        """Initialise the coordinator.

        Args:
            hass: The Home Assistant instance.
            config_entry: The config entry that owns this coordinator.
            cache: Shared static GTFS schedule cache.
        """
        super().__init__(
            hass,
            _logger,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL_SECONDS),
        )
        self._config_entry = config_entry
        self._cache = cache
        self._last_successful_fetch: datetime | None = None
        self._last_error_key: str | None = None

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch and parse the GTFS-RT trip updates JSON.

        Requests the feed URL stored in the config entry, parses the JSON
        FeedMessage into a list of trip-update dicts, and returns them under
        the ``entities`` key.

        Returns:
            A dict with a single key ``"entities"`` whose value is a list of
            parsed trip-update dicts.  Each dict has the following keys:

            - ``trip_id`` (str)
            - ``route_id`` (str)
            - ``direction_id`` (str | None)
            - ``start_date`` (str | None)
            - ``stop_time_updates`` (list[dict]) — each entry has ``stop_id``,
              ``arrival_delay``, ``departure_delay``, ``arrival_time``, and
              ``departure_time``.

        Raises:
            ConfigEntryAuthFailed: When the feed responds with HTTP 401.
            UpdateFailed: On any other HTTP error, network error, or JSON
                parse failure.
        """
        url = self._config_entry.data[CONF_TRIP_UPDATE_URL]
        api_key = self._config_entry.data[CONF_API_KEY]
        session = async_get_clientsession(self.hass)

        try:
            async with session.get(url, headers={"x-api-key": api_key}) as resp:
                if resp.status == 401:
                    self._log_once(
                        "http_401",
                        _logger.error,
                        "GTFS-RT feed returned HTTP 401 — re-authentication required",
                    )
                    self._config_entry.async_start_reauth(self.hass)
                    raise ConfigEntryAuthFailed("Invalid API key")

                if resp.status >= 400:
                    error_key = f"http_{resp.status}"
                    self._log_once(
                        error_key,
                        _logger.warning,
                        "GTFS-RT feed returned HTTP %s",
                        resp.status,
                    )
                    raise UpdateFailed(f"HTTP {resp.status}")

                raw = await resp.text()

        except (ConfigEntryAuthFailed, UpdateFailed):
            raise
        except aiohttp.ClientError as exc:
            self._log_once(
                "client_error",
                _logger.warning,
                "GTFS-RT network error: %s",
                exc,
            )
            raise UpdateFailed(str(exc)) from exc

        try:
            payload = json.loads(raw)
            parsed_entities = self._parse_feed(payload)
        except Exception as exc:  # noqa: BLE001
            self._log_once(
                "json_parse",
                _logger.error,
                "Failed to parse GTFS-RT JSON response: %s",
                exc,
            )
            raise UpdateFailed(f"JSON parse error: {exc}") from exc

        self._last_successful_fetch = datetime.now()
        self._last_error_key = None

        return {"entities": parsed_entities}

    def _log_once(
        self,
        error_key: str,
        log_fn: Any,
        msg: str,
        *args: Any,
    ) -> None:
        """Emit a log message only when the error key has changed.

        Compares ``error_key`` against ``_last_error_key`` and emits the
        message through ``log_fn`` only when they differ.  Updates
        ``_last_error_key`` to ``error_key`` after logging.

        Args:
            error_key: Short string identifying the error category (e.g.
                ``"http_401"``).
            log_fn: Callable with the standard ``logging`` signature, e.g.
                ``_logger.warning``.
            msg: ``logging``-style format string.
            *args: Positional arguments interpolated into ``msg``.
        """
        if error_key != self._last_error_key:
            log_fn(msg, *args)
            self._last_error_key = error_key

    @staticmethod
    def _parse_feed(payload: Any) -> list[dict[str, Any]]:
        """Parse a GTFS-RT FeedMessage dict into a list of trip-update dicts.

        Iterates over the ``entity`` array in ``payload``, extracts each
        ``trip_update`` block, and normalises it into a flat dict with typed
        fields.  Missing optional keys are replaced with ``None`` rather than
        raising.

        Args:
            payload: Parsed JSON object representing the GTFS-RT FeedMessage.

        Returns:
            List of trip-update dicts.  Each dict contains:

            - ``trip_id`` (str)
            - ``route_id`` (str)
            - ``direction_id`` (str | None)
            - ``start_date`` (str | None)
            - ``stop_time_updates`` (list[dict]) where each inner dict has:
              ``stop_id``, ``arrival_delay``, ``departure_delay``,
              ``arrival_time``, ``departure_time``.

        Raises:
            TypeError: If ``payload`` is not a dict (propagated to caller as
                ``UpdateFailed``).
            KeyError: If a required field is absent (propagated to caller as
                ``UpdateFailed``).
        """
        entities: list[dict[str, Any]] = []

        for entity in payload.get("entity", []):
            trip_update = entity.get("trip_update")
            if trip_update is None:
                continue

            trip = trip_update.get("trip", {})
            trip_id: str = str(trip.get("trip_id", ""))
            route_id: str = str(trip.get("route_id", ""))
            raw_direction = trip.get("direction_id")
            direction_id: str | None = (
                str(raw_direction) if raw_direction is not None else None
            )
            start_date: str | None = trip.get("start_date")

            stop_time_updates: list[dict[str, Any]] = []
            for stu in trip_update.get("stop_time_update", []):
                arrival = stu.get("arrival") or {}
                departure = stu.get("departure") or {}
                stop_time_updates.append(
                    {
                        "stop_id": str(stu.get("stop_id", "")),
                        "arrival_delay": _int_or_none(arrival.get("delay")),
                        "departure_delay": _int_or_none(departure.get("delay")),
                        "arrival_time": _int_or_none(arrival.get("time")),
                        "departure_time": _int_or_none(departure.get("time")),
                    }
                )

            entities.append(
                {
                    "trip_id": trip_id,
                    "route_id": route_id,
                    "direction_id": direction_id,
                    "start_date": start_date,
                    "stop_time_updates": stop_time_updates,
                }
            )

        return entities


def _int_or_none(value: Any) -> int | None:
    """Convert a value to ``int``, returning ``None`` when conversion fails.

    Args:
        value: Any value that may be cast to ``int``.

    Returns:
        Integer representation of ``value``, or ``None`` if ``value`` is
        ``None`` or cannot be cast.
    """
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
