"""Sensor entities for the TFI Live integration."""

import logging
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .__init__ import TfiLiveConfigEntry
from .const import (
    ATTR_DEPARTURES,
    ATTR_DIRECTION_ID,
    ATTR_LAST_UPDATED,
    ATTR_OPERATOR_ID,
    ATTR_ROUTE_ID,
    ATTR_STOP_ID,
    AVAILABILITY_WINDOW_SECONDS,
    CONF_SENSORS,
    DEP_DELAY_MINUTES,
    DEP_REALTIME_TIME,
    DEP_ROUTE_NAME,
    DEP_SCHEDULED_TIME,
    DEP_TRIP_ID,
    DOMAIN,
    MAX_DEPARTURES,
)
from .coordinator import TfiLiveCoordinator

_logger = logging.getLogger(__name__)

_DUBLIN_TZ = ZoneInfo("Europe/Dublin")

# Departures up to this many minutes past their effective time are still shown.
_GRACE_MINUTES = 5

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TfiLiveConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up TFI Live sensor entities from a config entry.

    Creates one ``TfiLiveSensor`` for each sensor configuration stored in the
    config entry and registers them with Home Assistant.

    Args:
        hass: The Home Assistant instance.
        entry: The config entry being set up.
        async_add_entities: Callback used to register new entities.
    """
    coordinator: TfiLiveCoordinator = entry.runtime_data
    entities = [
        TfiLiveSensor(coordinator, sensor_config, entry.entry_id)
        for sensor_config in entry.data[CONF_SENSORS]
    ]
    async_add_entities(entities, True)


class TfiLiveSensor(CoordinatorEntity[TfiLiveCoordinator], SensorEntity):
    """A sensor entity reporting minutes to the next departure for a stop/route.

    State is the floor of minutes until the next upcoming departure, derived
    from real-time GTFS-RT data enriched with scheduled times from the static
    GTFS cache.  When unavailable or when no service is found, state is
    ``None``.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    # DURATION requires non-negative values; minutes-to-departure can be negative
    _attr_device_class = None
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: TfiLiveCoordinator,
        sensor_config: dict[str, Any],
        entry_id: str,
    ) -> None:
        """Initialise the sensor with coordinator, config, and entry identity.

        Args:
            coordinator: The shared TFI Live data coordinator.
            sensor_config: Dict of sensor-level config values from the config
                entry (stop_id, route_id, direction_id, operator_id, name).
            entry_id: The config entry ID, used to build a stable unique_id.
        """
        super().__init__(coordinator)
        self._stop_id: str = sensor_config["stop_id"]
        self._route_id: str = sensor_config["route_id"]
        self._direction_id: int | None = sensor_config.get("direction_id")
        self._operator_id: str | None = sensor_config.get("operator_id")
        self._name: str = sensor_config["name"]
        self._entry_id: str = entry_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name="TFI Live",
            manufacturer="National Transport Authority",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def unique_id(self) -> str:
        """Return a stable unique ID for this sensor.

        Returns:
            A string combining the entry ID, stop, route, direction, and
            operator so that two sensors differing only in direction or
            operator receive distinct IDs.
        """
        dir_segment = "" if self._direction_id is None else str(self._direction_id)
        op_segment = self._operator_id or ""
        return (
            f"{self._entry_id}_{self._stop_id}_{self._route_id}"
            f"_{dir_segment}_{op_segment}"
        )

    @property
    def name(self) -> str:
        """Return the display name configured for this sensor.

        Returns:
            Human-readable sensor name from the config entry.
        """
        return self._name

    @property
    def available(self) -> bool:
        """Return True when coordinator data is fresh enough to be trusted.

        Considers the coordinator healthy when ``last_update_success`` is True
        and the last successful fetch occurred within
        ``AVAILABILITY_WINDOW_SECONDS``.

        Returns:
            ``True`` if the coordinator has delivered a successful update
            within the availability window; ``False`` otherwise.
        """
        if not self.coordinator.last_update_success:
            return False
        last_fetch = self.coordinator.last_successful_fetch
        if last_fetch is None:
            return False
        return (
            datetime.now() - last_fetch
        ).total_seconds() <= AVAILABILITY_WINDOW_SECONDS

    @property
    def native_value(self) -> int | None:
        """Return truncated minutes to the next departure, or None.

        Uses real-time departure time when available; falls back to scheduled
        time.  Returns ``None`` when the sensor is unavailable or no departures
        are found.  Truncation is toward zero: T = -1.3 min returns -1.

        Returns:
            Minutes (truncated toward zero, may be negative for overdue
            departures) or ``None``.
        """
        if not self.available:
            return None
        departures = self._get_departures()
        if not departures:
            return None
        effective_dt = departures[0]["_effective_dt"]
        delta = (effective_dt - datetime.now()).total_seconds()
        return int(delta / 60)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return supplementary attributes for this sensor.

        When unavailable all values are ``None``.  When available, returns the
        configured stop/route/direction/operator identifiers, the departures
        list (at most ``MAX_DEPARTURES`` entries), and an ISO 8601 timestamp of
        the last successful coordinator update.

        Returns:
            Dict with keys ``stop_id``, ``route_id``, ``direction_id``,
            ``operator_id``, ``departures``, and ``last_updated``.
        """
        if not self.available:
            return {
                ATTR_STOP_ID: None,
                ATTR_ROUTE_ID: None,
                ATTR_DIRECTION_ID: None,
                ATTR_OPERATOR_ID: None,
                ATTR_DEPARTURES: None,
                ATTR_LAST_UPDATED: None,
            }

        departures = self._get_departures()
        public_departures = [
            {
                DEP_SCHEDULED_TIME: dep[DEP_SCHEDULED_TIME],
                DEP_REALTIME_TIME: dep[DEP_REALTIME_TIME],
                DEP_DELAY_MINUTES: dep[DEP_DELAY_MINUTES],
                DEP_TRIP_ID: dep[DEP_TRIP_ID],
                DEP_ROUTE_NAME: dep[DEP_ROUTE_NAME],
            }
            for dep in departures
        ]

        return {
            ATTR_STOP_ID: self._stop_id,
            ATTR_ROUTE_ID: self._route_id,
            ATTR_DIRECTION_ID: self._direction_id,
            ATTR_OPERATOR_ID: self._operator_id,
            ATTR_DEPARTURES: public_departures,
            ATTR_LAST_UPDATED: (
                last_fetch.isoformat()
                if (last_fetch := self.coordinator.last_successful_fetch) is not None
                else None
            ),
        }

    def _get_departures(self) -> list[dict[str, Any]]:
        """Merge real-time and scheduled departures into a sorted, filtered list.

        Retrieves GTFS-RT trip updates from the coordinator and scheduled
        departures from the static cache, merges them on ``trip_id``, filters
        to those not yet departed (with a grace period of ``_GRACE_MINUTES``),
        and returns at most ``MAX_DEPARTURES`` entries sorted ascending by
        effective departure time.

        Each returned dict contains all five public keys plus an internal
        ``_effective_dt`` datetime used for sorting and state calculation.
        The caller strips the private key before surfacing to HA attributes.

        Returns:
            List of departure dicts.  May be empty.  Each dict has keys:
            ``scheduled_time``, ``realtime_time``, ``delay_minutes``,
            ``trip_id``, ``route_name``, and ``_effective_dt``.
        """
        coordinator_data = self.coordinator.data
        if not coordinator_data:
            return []

        entities = coordinator_data.get("entities", [])
        now = datetime.now()
        cutoff = now - timedelta(minutes=_GRACE_MINUTES)

        # Build a dict of RT departures keyed by trip_id.
        # direction_id in coordinator data is stored as str by _parse_feed.
        direction_filter: str | None = (
            str(self._direction_id) if self._direction_id is not None else None
        )

        rt_by_trip: dict[str, dict[str, Any]] = {}
        for entity in entities:
            if entity["route_id"] != self._route_id:
                continue
            if (
                direction_filter is not None
                and entity["direction_id"] != direction_filter
            ):
                continue
            # operator_id (agency_id) is not present in coordinator data; skip filter.
            trip_id: str = entity["trip_id"]
            for stu in entity["stop_time_updates"]:
                if stu["stop_id"] != self._stop_id:
                    continue
                # Prefer departure_time over arrival_time for the effective RT time.
                unix_ts: int | None = stu["departure_time"] or stu["arrival_time"]
                if unix_ts is None:
                    continue
                delay: int | None = stu["departure_delay"]
                rt_by_trip[trip_id] = {
                    "_unix_ts": unix_ts,
                    "_delay": delay,
                }
                # Only store the first matching stop_time_update per trip.
                break

        # Fetch scheduled departures from static cache.
        static_departures = self.coordinator._cache.get_scheduled_departures(
            self._stop_id,
            self._route_id,
            self._direction_id,
            self._operator_id,
            date.today(),
        )

        # Build a lookup from trip_id → (scheduled_time_hhmm, route_name).
        static_by_trip: dict[str, tuple[str, str | None]] = {
            trip_id: (sched_time, route_name)
            for trip_id, sched_time, route_name in static_departures
        }

        candidates: list[dict[str, Any]] = []

        # Process RT departures.
        for trip_id, rt_info in rt_by_trip.items():
            rt_unix_ts: int = rt_info["_unix_ts"]
            rt_delay: int | None = rt_info["_delay"]

            rt_dt = datetime.fromtimestamp(rt_unix_ts, tz=_DUBLIN_TZ).replace(
                tzinfo=None
            )
            rt_hhmm = rt_dt.strftime("%H:%M")

            sched_hhmm: str | None = None
            route_name: str | None = None
            if trip_id in static_by_trip:
                sched_hhmm, route_name = static_by_trip[trip_id]

            delay_minutes: int | None = (
                round(rt_delay / 60) if rt_delay is not None else None
            )

            candidates.append(
                {
                    DEP_SCHEDULED_TIME: sched_hhmm,
                    DEP_REALTIME_TIME: rt_hhmm,
                    DEP_DELAY_MINUTES: delay_minutes,
                    DEP_TRIP_ID: trip_id,
                    DEP_ROUTE_NAME: route_name,
                    "_effective_dt": rt_dt,
                }
            )

        # Process static-only departures (trip not present in RT data).
        for trip_id, sched_hhmm, route_name in static_departures:
            if trip_id in rt_by_trip:
                continue
            sched_dt = _parse_hhmm_today(sched_hhmm)
            candidates.append(
                {
                    DEP_SCHEDULED_TIME: sched_hhmm,
                    DEP_REALTIME_TIME: None,
                    DEP_DELAY_MINUTES: None,
                    DEP_TRIP_ID: trip_id,
                    DEP_ROUTE_NAME: route_name,
                    "_effective_dt": sched_dt,
                }
            )

        # Filter out departures that have passed beyond the grace window.
        candidates = [c for c in candidates if c["_effective_dt"] >= cutoff]

        # Sort ascending by effective departure time.
        candidates.sort(key=lambda c: c["_effective_dt"])

        return candidates[:MAX_DEPARTURES]


def _parse_hhmm_today(hhmm: str) -> datetime:
    """Parse an ``HH:MM`` string into a naive datetime for today.

    Combines the given time with today's date to produce a naive datetime
    suitable for arithmetic against ``datetime.now()``.

    Args:
        hhmm: Time string in ``HH:MM`` format.

    Returns:
        A naive ``datetime`` representing ``hhmm`` on today's date.
    """
    today = date.today()
    hour, minute = int(hhmm[:2]), int(hhmm[3:5])
    return datetime(today.year, today.month, today.day, hour, minute)
