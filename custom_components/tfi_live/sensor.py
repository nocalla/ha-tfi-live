"""Sensor entities for the TFI Live integration."""

import logging
from datetime import UTC, datetime, timedelta
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
    ATTR_NEXT_DEPARTURE_ROUTE_NAME,
    ATTR_OPERATOR_ID,
    ATTR_ROUTE_ID,
    ATTR_STOP_ID,
    AVAILABILITY_WINDOW_SECONDS,
    CONF_NUM_DEPARTURES,
    CONF_SENSORS,
    DEFAULT_NUM_DEPARTURES,
    DEP_DELAY_MINUTES,
    DEP_REALTIME_TIME,
    DEP_ROUTE_NAME,
    DEP_SCHEDULED_TIME,
    DEP_TRIP_ID,
    DOMAIN,
)
from .coordinator import TfiLiveCoordinator

_logger = logging.getLogger(__name__)

_DUBLIN_TZ = ZoneInfo("Europe/Dublin")

# Departures up to this many minutes past their effective time are still shown.
_GRACE_MINUTES = 5

PARALLEL_UPDATES = 0


def _now_dublin() -> datetime:
    """Return the current time as a naive datetime in Dublin local time.

    Returns:
        Naive datetime equivalent to ``datetime.now()`` in Europe/Dublin.
    """
    return datetime.now(_DUBLIN_TZ).replace(tzinfo=None)


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
    num_departures: int = entry.data.get(CONF_NUM_DEPARTURES, DEFAULT_NUM_DEPARTURES)
    entities = [
        TfiLiveSensor(coordinator, sensor_config, entry.entry_id, num_departures)
        for sensor_config in entry.data[CONF_SENSORS]
    ]
    async_add_entities(entities, True)


class TfiLiveSensor(CoordinatorEntity[TfiLiveCoordinator], SensorEntity):
    """A sensor entity reporting minutes to the next departure for a stop/route.

    State is the floor of minutes until the next upcoming departure, derived
    from real-time GTFS-RT data enriched with scheduled times from the static
    GTFS cache.  When unavailable or when no service is found, state is
    ``None``.

    When ``route_id`` is unset (stop-wide monitoring), departures are merged
    and sorted across every route serving the stop, and
    ``next_departure_route_name`` identifies which route the reported
    departure belongs to.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    # IQS `entity-device-class` (Platinum): no SensorDeviceClass fits — DURATION
    # requires non-negative values, but minutes-to-departure goes negative for
    # an overdue service. Accepted exception to the rule, not an oversight.
    _attr_device_class = None
    _attr_has_entity_name = True
    # Drives the icon lookup in icons.json; entity names come from user config
    _attr_translation_key = "next_departure"

    def __init__(
        self,
        coordinator: TfiLiveCoordinator,
        sensor_config: dict[str, Any],
        entry_id: str,
        num_departures: int = DEFAULT_NUM_DEPARTURES,
    ) -> None:
        """Initialise the sensor with coordinator, config, and entry identity.

        Args:
            coordinator: The shared TFI Live data coordinator.
            sensor_config: Dict of sensor-level config values from the config
                entry (stop_id, route_id, direction_id, operator_id, name).
            entry_id: The config entry ID, used to build a stable unique_id.
            num_departures: Maximum number of entries to report in the
                ``departures`` attribute, from the config entry's
                ``CONF_NUM_DEPARTURES`` (defaulting for pre-#115 entries).
        """
        super().__init__(coordinator)
        self._stop_id: str = sensor_config["stop_id"]
        self._route_id: str | None = sensor_config.get("route_id")
        self._direction_id: int | None = sensor_config.get("direction_id")
        self._operator_id: str | None = sensor_config.get("operator_id")
        self._name: str = sensor_config["name"]
        self._entry_id: str = entry_id
        self._num_departures: int = num_departures
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
            datetime.now(UTC) - last_fetch
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
        delta = (effective_dt - _now_dublin()).total_seconds()
        return int(delta / 60)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return supplementary attributes for this sensor.

        When unavailable all values are ``None``.  When available, returns the
        configured stop/route/direction/operator identifiers, the departures
        list (at most ``self._num_departures`` entries), and an ISO 8601
        timestamp of the last successful coordinator update.

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
                ATTR_NEXT_DEPARTURE_ROUTE_NAME: None,
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
            ATTR_NEXT_DEPARTURE_ROUTE_NAME: (
                departures[0][DEP_ROUTE_NAME] if departures else None
            ),
        }

    def _carry_forward_delay(self, entity: dict[str, Any], trip_id: str) -> int | None:
        """Find the nearest preceding stop's delay when our stop is unlisted.

        TFI's RT feed sometimes omits our configured stop from a tracked
        trip's ``stop_time_updates`` even while reporting live delay at
        neighboring stops. Per the GTFS-RT spec, an unlisted stop inherits
        the last preceding stop's delay, so this walks the entity's updates
        in feed order (GTFS-RT requires these sorted by ``stop_sequence``)
        and returns the delay from the closest stop at or before our stop's
        position in the trip's static stop pattern.

        Args:
            entity: A coordinator trip-update entity dict, containing
                ``stop_time_updates``.
            trip_id: GTFS trip ID the entity belongs to.

        Returns:
            The carried-forward delay in seconds, or ``None`` if the trip's
            static stop pattern is unavailable, our stop isn't in it, or no
            preceding update in the feed carries an explicit delay.
        """
        static_stops = self.coordinator.cache.get_trip_stops(trip_id)
        if not static_stops:
            return None

        target_seq: int | None = None
        seq_by_stop: dict[str, int] = {}
        for seq, stop_id in static_stops:
            seq_by_stop[stop_id] = seq
            if stop_id == self._stop_id:
                target_seq = seq
        if target_seq is None:
            return None

        carried_delay: int | None = None
        for stu in entity["stop_time_updates"]:
            mapped_seq = seq_by_stop.get(stu["stop_id"])
            if mapped_seq is None:
                continue
            if mapped_seq > target_seq:
                break
            delay = (
                stu["departure_delay"]
                if stu["departure_delay"] is not None
                else stu["arrival_delay"]
            )
            if delay is not None:
                carried_delay = delay

        return carried_delay

    def _get_departures(self) -> list[dict[str, Any]]:
        """Merge real-time and scheduled departures into a sorted, filtered list.

        Retrieves GTFS-RT trip updates from the coordinator and scheduled
        departures from the static cache, merges them on ``trip_id``, filters
        to those not yet departed (with a grace period of ``_GRACE_MINUTES``),
        and returns at most ``self._num_departures`` entries sorted ascending
        by effective departure time.

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
        now = _now_dublin()
        cutoff = now - timedelta(minutes=_GRACE_MINUTES)

        # Build a dict of RT departures keyed by trip_id.
        # direction_id in coordinator data is stored as str by _parse_feed.
        direction_filter: str | None = (
            str(self._direction_id) if self._direction_id is not None else None
        )

        # Fetch scheduled departures from static cache first — the RT loop
        # below needs each trip's scheduled time to derive an effective time
        # for delay-only stop_time_updates (entries that carry a delay but no
        # absolute arrival/departure epoch; TFI's feed does this routinely).
        static_departures = self.coordinator.cache.get_scheduled_departures(
            self._stop_id,
            self._route_id,
            self._direction_id,
            self._operator_id,
            datetime.now(_DUBLIN_TZ).date(),
        )

        # Build a lookup from trip_id → (scheduled_time_hhmm, route_name).
        static_by_trip: dict[str, tuple[str, str | None]] = {
            trip_id: (sched_time, route_name)
            for trip_id, sched_time, route_name in static_departures
        }

        rt_by_trip: dict[str, dict[str, Any]] = {}
        for entity in entities:
            if self._route_id is not None and entity["route_id"] != self._route_id:
                continue
            if (
                direction_filter is not None
                and entity["direction_id"] != direction_filter
            ):
                continue
            # operator_id (agency_id) is not present in coordinator data; skip filter.
            trip_id = entity["trip_id"]
            exact_match = False
            for stu in entity["stop_time_updates"]:
                if stu["stop_id"] != self._stop_id:
                    continue
                exact_match = True
                # Prefer departure over arrival, keeping each event's time
                # paired with its own delay.
                if stu["departure_time"] is not None:
                    unix_ts: int | None = stu["departure_time"]
                    delay: int | None = stu["departure_delay"]
                elif stu["arrival_time"] is not None:
                    unix_ts = stu["arrival_time"]
                    delay = stu["arrival_delay"]
                else:
                    unix_ts = None
                    delay = (
                        stu["departure_delay"]
                        if stu["departure_delay"] is not None
                        else stu["arrival_delay"]
                    )

                rt_dt: datetime | None
                if unix_ts is not None:
                    rt_dt = datetime.fromtimestamp(unix_ts, tz=_DUBLIN_TZ).replace(
                        tzinfo=None
                    )
                elif delay is not None and trip_id in static_by_trip:
                    # No absolute time in the feed for this stop — fall back
                    # to the static schedule offset by the reported delay.
                    fallback_sched_hhmm, _ = static_by_trip[trip_id]
                    rt_dt = _parse_hhmm_today(fallback_sched_hhmm) + timedelta(
                        seconds=delay
                    )
                else:
                    rt_dt = None

                if rt_dt is None:
                    continue

                rt_by_trip[trip_id] = {
                    "_dt": rt_dt,
                    "_delay": delay,
                }
                # Only store the first matching stop_time_update per trip.
                break

            if exact_match or trip_id not in static_by_trip:
                continue

            # TFI's RT feed routinely omits our stop from a tracked trip's
            # update list even while reporting live delay at neighboring
            # stops. Per the GTFS-RT spec (and TFI's own app), an unlisted
            # stop inherits the last preceding stop's delay.
            carried_delay = self._carry_forward_delay(entity, trip_id)
            if carried_delay is None:
                continue

            fallback_sched_hhmm, _ = static_by_trip[trip_id]
            rt_by_trip[trip_id] = {
                "_dt": _parse_hhmm_today(fallback_sched_hhmm)
                + timedelta(seconds=carried_delay),
                "_delay": carried_delay,
            }

        candidates: list[dict[str, Any]] = []

        # Process RT departures.
        for trip_id, rt_info in rt_by_trip.items():
            rt_dt = rt_info["_dt"]
            rt_delay: int | None = rt_info["_delay"]

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

        return candidates[: self._num_departures]


def _parse_hhmm_today(hhmm: str) -> datetime:
    """Parse an ``HH:MM`` string into a naive Dublin-local datetime for today.

    Combines the given time with Dublin's current date to produce a naive
    datetime suitable for arithmetic against ``_now_dublin()``.

    Args:
        hhmm: Time string in ``HH:MM`` format.

    Returns:
        A naive ``datetime`` representing ``hhmm`` on Dublin's current date.
    """
    today = datetime.now(_DUBLIN_TZ).date()
    hour, minute = int(hhmm[:2]), int(hhmm[3:5])
    return datetime(today.year, today.month, today.day, hour, minute)
