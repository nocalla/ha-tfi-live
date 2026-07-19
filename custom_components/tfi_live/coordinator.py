"""DataUpdateCoordinator for the TFI Live integration."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from nta_gtfs import (
    GtfsRtAuthError,
    GtfsRtClient,
    GtfsRtFetchError,
    GtfsRtParseError,
    StaticGtfsClient,
    StaticGtfsLoadError,
)

from .const import (
    CONF_ROUTE_ID,
    CONF_SENSORS,
    CONF_STOP_ID,
    DOMAIN,
    STATIC_GTFS_REFRESH_HOURS,
    UPDATE_INTERVAL_SECONDS,
)

_logger = logging.getLogger(__name__)


class TfiLiveCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator that fetches and parses the NTA GTFS-RT trip updates feed."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        rt_client: GtfsRtClient,
        cache: StaticGtfsClient,
    ) -> None:
        """Initialise the coordinator.

        Args:
            hass: The Home Assistant instance.
            config_entry: The config entry that owns this coordinator.
            rt_client: Pre-constructed GTFS-RT client used to fetch trip updates.
            cache: Shared static GTFS schedule cache.
        """
        super().__init__(
            hass,
            _logger,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL_SECONDS),
        )
        self._config_entry = config_entry
        self._rt_client = rt_client
        self._cache = cache
        self._last_successful_fetch: datetime | None = None
        self._last_error_key: str | None = None
        self._static_refresh_task: asyncio.Task[None] | None = None

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch and parse the GTFS-RT trip updates JSON.

        Calls the GTFS-RT client to retrieve trip updates, converts each
        ``TripUpdate`` object into a dict, and returns them under the
        ``entities`` key.

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
                Carries ``translation_key="invalid_api_key"``.
            UpdateFailed: On any other HTTP error, network error, or parse
                failure. Carries ``translation_key`` of ``"cannot_connect"``
                or ``"cannot_parse"`` depending on the failure kind.
        """
        # Never schedule the memory-heavy static load before the first
        # successful refresh — a setup-retry loop would re-trigger it on
        # every retry and OOM-kill low-memory hosts (#100).  After that,
        # schedule even when the RT fetch below fails so schedule data
        # stays fresh through an RT feed outage.
        first_success_pending = self._last_successful_fetch is None
        if not first_success_pending:
            self.async_schedule_static_refresh()

        try:
            trip_updates = await self._rt_client.async_fetch_trip_updates()
        except GtfsRtAuthError as exc:
            self._log_once(
                "http_401",
                _logger.error,
                "GTFS-RT feed returned HTTP 401 — re-authentication required",
            )
            self._config_entry.async_start_reauth(self.hass)
            raise ConfigEntryAuthFailed(
                "Invalid API key",
                translation_domain=DOMAIN,
                translation_key="invalid_api_key",
            ) from exc
        except GtfsRtFetchError as exc:
            self._log_once(
                "client_error",
                _logger.warning,
                "GTFS-RT network error: %s",
                exc,
            )
            raise UpdateFailed(
                str(exc),
                translation_domain=DOMAIN,
                translation_key="cannot_connect",
                translation_placeholders={"error": str(exc)},
            ) from exc
        except GtfsRtParseError as exc:
            self._log_once(
                "parse_error",
                _logger.error,
                "Failed to parse GTFS-RT response: %s",
                exc,
            )
            raise UpdateFailed(
                str(exc),
                translation_domain=DOMAIN,
                translation_key="cannot_parse",
                translation_placeholders={"error": str(exc)},
            ) from exc

        entities = [
            {
                "trip_id": tu.trip_id,
                "route_id": tu.route_id,
                "direction_id": tu.direction_id,
                "start_date": tu.start_date,
                "stop_time_updates": [
                    {
                        "stop_id": stu.stop_id,
                        "arrival_delay": stu.arrival_delay,
                        "departure_delay": stu.departure_delay,
                        "arrival_time": stu.arrival_time,
                        "departure_time": stu.departure_time,
                    }
                    for stu in tu.stop_time_updates
                ],
            }
            for tu in trip_updates
        ]

        self._last_successful_fetch = datetime.now(UTC)
        self._last_error_key = None

        # Covers the first successful refresh, which the guard above skips.
        if first_success_pending:
            self.async_schedule_static_refresh()

        return {"entities": entities}

    def async_schedule_static_refresh(self) -> None:
        """Schedule a background refresh of the static GTFS data when stale.

        The static GTFS archive is large (~80 MB) and takes minutes to
        download and parse, so it is never awaited inline — the refresh runs
        as a config-entry background task and sensors pick up the schedule
        data on the next coordinator update after it completes.  Does nothing
        when the data is still fresh, a refresh is already in flight, or HA
        is still starting up — the load peaks memory usage and has OOM-killed
        HA core on low-memory hosts when run during startup (#100); the next
        coordinator update after startup completes picks it up instead.
        """
        if self.hass.state is not CoreState.running:
            return

        task = self._static_refresh_task
        if task is not None and not task.done():
            return

        loaded_at = self._cache.loaded_at
        if loaded_at is not None and datetime.now(UTC) - loaded_at < timedelta(
            hours=STATIC_GTFS_REFRESH_HOURS
        ):
            return

        self._static_refresh_task = self._config_entry.async_create_background_task(
            self.hass,
            self._async_refresh_static(),
            name=f"{DOMAIN} static GTFS refresh",
        )

    async def _async_refresh_static(self) -> None:
        """Refresh the static GTFS cache, logging instead of raising on failure.

        Failures are logged and swallowed so a broken static feed never takes
        down the coordinator — sensors keep working from real-time data alone
        and the refresh is retried on a later coordinator update.
        """
        try:
            await self._cache.async_refresh_if_stale()
        except StaticGtfsLoadError as exc:
            _logger.warning(
                "Static GTFS load failed: %s — schedule data unavailable until"
                " the next successful load",
                exc,
            )
        else:
            _logger.info("Static GTFS data loaded")
            self._async_check_unmatched_pairs()

    def _async_check_unmatched_pairs(self) -> None:
        """Raise or clear Repairs issues for stop/route pairs absent from the schedule.

        Groups configured sensors by unique ``(stop_id, route_id)`` pair and
        checks each against ``cache.has_scheduled_pair``, which ignores
        calendar/direction/operator filtering entirely. A pair that never
        appears in the static schedule at all is a misconfiguration (e.g. a
        stop_code or route_short_name entered where a real stop_id/route_id
        is required, #102) rather than "no departures right now", so it
        raises a warning-level Repairs issue naming every affected sensor.
        Pairs found present have any previously-raised issue for them
        cleared, since a later config fix or a route only appearing in a
        later feed should make the issue disappear automatically. A pair
        that no longer appears in the config at all — because the sensor
        was edited to a different stop/route or removed outright — is
        reconciled separately by comparing against the issue registry
        directly, since it never reaches the per-pair loop below. Does
        nothing until the cache has completed its first successful load, to
        avoid flagging every configured pair as unmatched before there is
        any schedule data to check against. Stop-wide sensors (``route_id``
        unset) are skipped entirely — ``has_scheduled_pair`` requires a real
        route_id, and there's no equivalent misconfiguration to detect for
        a sensor that merges every route already known to serve the stop.
        """
        if not self._cache.available:
            return

        pairs: dict[tuple[str, str], list[str]] = {}
        for sensor_config in self._config_entry.options.get(CONF_SENSORS, []):
            route_id: str | None = sensor_config[CONF_ROUTE_ID]
            if route_id is None:
                # Stop-wide sensors have no single route to check for a
                # scheduled pair against; has_scheduled_pair requires a real
                # route_id, and there's no equivalent misconfiguration to
                # detect here since the stop-wide "route" always matches
                # whatever the stop actually serves.
                continue
            stop_id: str = sensor_config[CONF_STOP_ID]
            pairs.setdefault((stop_id, route_id), []).append(sensor_config["name"])

        entry_id = self._config_entry.entry_id
        current_unmatched_ids: set[str] = set()

        for (stop_id, route_id), sensor_names in pairs.items():
            issue_id = f"{entry_id}_{stop_id}_{route_id}"
            if self._cache.has_scheduled_pair(stop_id, route_id):
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            else:
                current_unmatched_ids.add(issue_id)
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    issue_id,
                    is_fixable=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key="unmatched_stop_route_pair",
                    translation_placeholders={
                        "stop_id": stop_id,
                        "route_id": route_id,
                        "sensor_names": ", ".join(sensor_names),
                    },
                )

        # Clear issues previously raised by this entry for pairs that have
        # since dropped out of the config entirely (edited or removed) —
        # querying the registry directly rather than tracking state across
        # coordinator instances means this self-heals across entry reloads.
        stale_prefix = f"{entry_id}_"
        registry_issues = ir.async_get(self.hass).issues
        for domain, issue_id in list(registry_issues):
            if (
                domain == DOMAIN
                and issue_id.startswith(stale_prefix)
                and issue_id not in current_unmatched_ids
            ):
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)

    @property
    def last_successful_fetch(self) -> datetime | None:
        """Return the datetime of the last successful data fetch, or None.

        Returns:
            A ``datetime`` instance set when the most recent feed request
            completed successfully, or ``None`` if no fetch has succeeded yet.
        """
        return self._last_successful_fetch

    @property
    def cache(self) -> StaticGtfsClient:
        """Return the shared static GTFS schedule cache.

        Returns:
            The ``StaticGtfsClient`` instance shared by all sensors for this
            coordinator.
        """
        return self._cache

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
