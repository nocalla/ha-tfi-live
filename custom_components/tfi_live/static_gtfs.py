"""Static GTFS schedule cache for the TFI Live integration."""

import csv
import io
import logging
import zipfile
from datetime import date, datetime, timedelta

import aiohttp

from .const import STATIC_GTFS_REFRESH_HOURS

_logger = logging.getLogger(__name__)

_WEEKDAY_COLUMNS = [
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
]


class StaticGtfsCache:
    """In-memory cache of static GTFS schedule data."""

    def __init__(self, static_gtfs_url: str, session: aiohttp.ClientSession) -> None:
        """Initialise the cache with a download URL and HTTP session.

        Args:
            static_gtfs_url: URL of the static GTFS zip to download.
            session: aiohttp client session used for the download request.
        """
        self._url = static_gtfs_url
        self._session = session
        self.available: bool = False
        self._loaded_at: datetime | None = None
        self._in_error_state: bool = False

        self._routes_by_id: dict[str, dict[str, str]] = {}
        self._trips_by_id: dict[str, dict[str, str]] = {}
        self._calendar: list[dict[str, str]] = []
        self._calendar_dates: list[dict[str, str]] = []
        # keyed by (stop_id, route_short_name)
        # values: list of (trip_id, departure_time_raw, direction_id, agency_id_or_none)
        self._departure_index: dict[
            tuple[str, str], list[tuple[str, str, str, str | None]]
        ] = {}

    async def async_load(self) -> None:
        """Download and parse the static GTFS zip into in-memory dicts.

        Downloads the zip from the configured URL, extracts it in memory
        (no disk writes), and builds pure-Python lookup structures for routes,
        trips, calendar, calendar_dates, and a pre-joined departure index.

        On any download or parse failure the cache is marked unavailable and
        a WARNING is logged exactly once per failure run (deduplicated via
        ``_in_error_state``).  On success ``available`` is set to ``True``.
        """
        try:
            async with self._session.get(self._url) as resp:
                if not resp.ok:
                    self._handle_error(
                        "Static GTFS download failed: HTTP %s from %s",
                        resp.status,
                        self._url,
                    )
                    return
                content = await resp.read()
        except aiohttp.ClientError as exc:
            self._handle_error(
                "Static GTFS download error for %s: %s",
                self._url,
                exc,
            )
            return

        try:
            self._parse_zip(content)
        except Exception as exc:  # noqa: BLE001
            self._handle_error("Static GTFS parse error: %s", exc)
            return

        self.available = True
        self._loaded_at = datetime.now()
        self._in_error_state = False

    def _handle_error(self, msg: str, *args: object) -> None:
        """Log a WARNING once per failure run and mark the cache unavailable.

        Emits the log message only when not already in an error state, then
        sets ``available`` to ``False`` and ``_in_error_state`` to ``True``.

        Args:
            msg: ``logging``-style format string.
            *args: Positional arguments interpolated into ``msg``.
        """
        if not self._in_error_state:
            _logger.warning(msg, *args)
            self._in_error_state = True
        self.available = False

    def _parse_zip(self, content: bytes) -> None:
        """Extract a GTFS zip from raw bytes and populate internal data structures.

        Reads the following files from the zip:
        ``stops.txt``, ``routes.txt``, ``trips.txt``, ``stop_times.txt``,
        ``calendar.txt``, ``calendar_dates.txt`` (optional).

        Builds ``_routes_by_id``, ``_trips_by_id``, ``_calendar``,
        ``_calendar_dates``, and ``_departure_index`` using Python's
        ``csv.DictReader`` — no third-party libraries required.

        Args:
            content: Raw bytes of the GTFS zip archive.

        Raises:
            KeyError: If a required file is missing from the zip.
            Exception: If any CSV cannot be parsed.
        """
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            names = set(zf.namelist())

            def _read_csv(filename: str) -> list[dict[str, str]]:
                """Read a CSV file from the open zip into a list of row dicts.

                Args:
                    filename: Name of the file inside the zip archive.

                Returns:
                    List of row dicts with string values; BOM-stripped headers.
                """
                with zf.open(filename) as fh:
                    text = io.TextIOWrapper(fh, encoding="utf-8-sig")
                    return list(csv.DictReader(text))

            routes_rows = _read_csv("routes.txt")
            trips_rows = _read_csv("trips.txt")
            stop_times_rows = _read_csv("stop_times.txt")
            calendar_rows = _read_csv("calendar.txt")

            if "calendar_dates.txt" in names:
                calendar_dates_rows = _read_csv("calendar_dates.txt")
            else:
                calendar_dates_rows = []

            # Build routes index: route_id → {route_short_name, agency_id}
            self._routes_by_id = {
                row["route_id"]: {
                    "route_short_name": row.get("route_short_name", ""),
                    "agency_id": row.get("agency_id", ""),
                }
                for row in routes_rows
                if "route_id" in row
            }

            # Build trips index: trip_id → {route_id, direction_id, service_id}
            self._trips_by_id = {
                row["trip_id"]: {
                    "route_id": row.get("route_id", ""),
                    "direction_id": row.get("direction_id", ""),
                    "service_id": row.get("service_id", ""),
                }
                for row in trips_rows
                if "trip_id" in row
            }

            # Store calendar and calendar_dates as plain lists
            self._calendar = calendar_rows
            self._calendar_dates = calendar_dates_rows

            # Build the departure index at parse time: join stop_times → trips → routes
            departure_index: dict[
                tuple[str, str], list[tuple[str, str, str, str | None]]
            ] = {}

            for st_row in stop_times_rows:
                stop_id = st_row.get("stop_id", "")
                trip_id = st_row.get("trip_id", "")
                departure_time_raw = st_row.get("departure_time", "")

                trip_info = self._trips_by_id.get(trip_id)
                if trip_info is None:
                    continue

                route_id = trip_info["route_id"]
                direction_id = trip_info["direction_id"]

                route_info = self._routes_by_id.get(route_id)
                if route_info is None:
                    continue

                route_short_name = route_info["route_short_name"]
                agency_id_raw = route_info["agency_id"]
                agency_id: str | None = agency_id_raw if agency_id_raw else None

                key = (stop_id, route_short_name)
                if key not in departure_index:
                    departure_index[key] = []
                departure_index[key].append(
                    (trip_id, departure_time_raw, direction_id, agency_id)
                )

            self._departure_index = departure_index

    def _active_service_ids(self, target_date: date) -> set[str]:
        """Return the set of service IDs running on ``target_date``.

        Applies ``calendar`` (regular schedule with weekday flags and date
        range) and ``calendar_dates`` (added/removed exceptions) to determine
        which services are in effect.

        Args:
            target_date: The date for which to evaluate service validity.

        Returns:
            Set of GTFS ``service_id`` strings active on the given date.
        """
        date_str = target_date.strftime("%Y%m%d")
        weekday_col = _WEEKDAY_COLUMNS[target_date.weekday()]

        active: set[str] = set(
            row["service_id"]
            for row in self._calendar
            if (
                "service_id" in row
                and "start_date" in row
                and "end_date" in row
                and weekday_col in row
                and row[weekday_col] == "1"
                and row["start_date"] <= date_str
                and row["end_date"] >= date_str
            )
        )

        for row in self._calendar_dates:
            if row.get("date") == date_str:
                sid = row.get("service_id", "")
                if row.get("exception_type") == "1":
                    active.add(sid)
                elif row.get("exception_type") == "2":
                    active.discard(sid)

        return active

    @staticmethod
    def _normalise_time(raw: str) -> str:
        """Convert a GTFS departure time string to ``HH:MM`` format.

        GTFS times may exceed 23:59:59 for trips running after midnight
        (e.g. ``25:30:00``).  Hours are wrapped modulo 24 so the returned
        string is always a valid ``HH:MM`` clock time.

        Args:
            raw: Raw GTFS time string, e.g. ``"08:15:00"`` or ``"25:30:00"``.

        Returns:
            Time string in ``HH:MM`` format with hours in ``[0, 23]``.
        """
        parts = raw.strip().split(":")
        hour = int(parts[0]) % 24
        minute = parts[1] if len(parts) > 1 else "00"
        return f"{hour:02d}:{minute}"

    def get_scheduled_departures(
        self,
        stop_id: str,
        route_id: str,
        direction_id: int | None,
        operator_id: str | None,
        target_date: date,
    ) -> list[tuple[str, str, str | None]]:
        """Return scheduled departures for a stop/route on a given date.

        Looks up the pre-built ``_departure_index`` by ``(stop_id,
        route_short_name)`` and filters by active service IDs, optional
        ``direction_id``, and optional ``agency_id``.

        Args:
            stop_id: GTFS stop ID to filter on.
            route_id: GTFS route short name (e.g. ``"46A"``) to match against
                ``routes.route_short_name``.
            direction_id: GTFS direction filter (``0`` or ``1``); ``None``
                means no direction filter is applied.
            operator_id: GTFS agency ID to filter on; ``None`` means no
                agency filter is applied.
            target_date: The date for which to return scheduled departures.

        Returns:
            List of ``(trip_id, departure_time_str, route_short_name)`` tuples
            sorted ascending by ``departure_time_str`` (``HH:MM`` format).
            Returns an empty list when the cache is unavailable.
        """
        if not self.available:
            return []

        active_services = self._active_service_ids(target_date)
        if not active_services:
            return []

        candidates = self._departure_index.get((stop_id, route_id), [])
        if not candidates:
            return []

        direction_str: str | None = (
            str(direction_id) if direction_id is not None else None
        )

        results: list[tuple[str, str, str | None]] = []
        for trip_id, departure_time_raw, dep_direction_id, agency_id in candidates:
            trip_info = self._trips_by_id.get(trip_id)
            if trip_info is None:
                continue
            if trip_info["service_id"] not in active_services:
                continue
            if direction_str is not None and dep_direction_id != direction_str:
                continue
            if operator_id is not None and agency_id != operator_id:
                continue
            time_hhmm = self._normalise_time(departure_time_raw)
            results.append((trip_id, time_hhmm, route_id))

        results.sort(key=lambda t: t[1])
        return results

    async def async_refresh_if_stale(self) -> None:
        """Reload static GTFS data if the cache is absent or older than 24 hours.

        Calls ``async_load`` when ``_loaded_at`` is ``None`` or when more than
        ``STATIC_GTFS_REFRESH_HOURS`` hours have elapsed since the last
        successful load.  Does nothing when the cache is still fresh.
        """
        if self._loaded_at is None or (datetime.now() - self._loaded_at) > timedelta(
            hours=STATIC_GTFS_REFRESH_HOURS
        ):
            await self.async_load()
