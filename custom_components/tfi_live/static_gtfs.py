"""Static GTFS schedule cache for the TFI Live integration."""

import io
import logging
import zipfile
from datetime import date, datetime, timedelta

import aiohttp
import pandas as pd

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

        self._stops: pd.DataFrame = pd.DataFrame()
        self._routes: pd.DataFrame = pd.DataFrame()
        self._trips: pd.DataFrame = pd.DataFrame()
        self._stop_times: pd.DataFrame = pd.DataFrame()
        self._calendar: pd.DataFrame = pd.DataFrame()
        self._calendar_dates: pd.DataFrame = pd.DataFrame()

    async def async_load(self) -> None:
        """Download and parse the static GTFS zip into in-memory DataFrames.

        Downloads the zip from the configured URL, extracts it in memory
        (no disk writes), and builds DataFrames for stops, routes, trips,
        stop_times, calendar, and calendar_dates.

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
        """Extract a GTFS zip from raw bytes and populate internal DataFrames.

        Reads the following files from the zip:
        ``stops.txt``, ``routes.txt``, ``trips.txt``, ``stop_times.txt``,
        ``calendar.txt``, ``calendar_dates.txt`` (optional).

        Args:
            content: Raw bytes of the GTFS zip archive.

        Raises:
            KeyError: If a required file is missing from the zip.
            Exception: If any CSV cannot be parsed.
        """
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            names = set(zf.namelist())

            def _read(filename: str) -> pd.DataFrame:
                """Read a CSV file from the open zip into a DataFrame.

                Args:
                    filename: Name of the file inside the zip archive.

                Returns:
                    Parsed DataFrame with string dtypes.
                """
                with zf.open(filename) as fh:
                    return pd.read_csv(
                        fh,
                        dtype=str,
                        encoding="utf-8-sig",
                        keep_default_na=False,
                    )

            self._stops = _read("stops.txt")
            self._routes = _read("routes.txt")
            self._trips = _read("trips.txt")
            self._stop_times = _read("stop_times.txt")
            self._calendar = _read("calendar.txt")

            if "calendar_dates.txt" in names:
                self._calendar_dates = _read("calendar_dates.txt")
            else:
                self._calendar_dates = pd.DataFrame(
                    columns=["service_id", "date", "exception_type"]
                )

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

        active: set[str] = set()

        if not self._calendar.empty and all(
            col in self._calendar.columns
            for col in ("service_id", "start_date", "end_date", weekday_col)
        ):
            mask = (
                (self._calendar[weekday_col] == "1")
                & (self._calendar["start_date"] <= date_str)
                & (self._calendar["end_date"] >= date_str)
            )
            active = set(self._calendar.loc[mask, "service_id"].tolist())

        if not self._calendar_dates.empty and all(
            col in self._calendar_dates.columns
            for col in ("service_id", "date", "exception_type")
        ):
            exceptions = self._calendar_dates[
                self._calendar_dates["date"] == date_str
            ]
            for _, row in exceptions.iterrows():
                if row["exception_type"] == "1":
                    active.add(row["service_id"])
                elif row["exception_type"] == "2":
                    active.discard(row["service_id"])

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

        Joins ``stop_times`` → ``trips`` → ``routes`` and filters by
        ``stop_id``, route short name, optional ``direction_id``, optional
        ``agency_id``, and service validity on ``target_date``.

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

        st = self._stop_times
        tr = self._trips
        ro = self._routes

        if st.empty or tr.empty or ro.empty:
            return []

        merged = st.merge(tr, on="trip_id", how="inner")
        merged = merged.merge(ro, on="route_id", how="inner")

        mask = (
            (merged["stop_id"] == stop_id)
            & (merged["route_short_name"] == route_id)
            & (merged["service_id"].isin(active_services))
        )

        if direction_id is not None:
            mask &= merged["direction_id"] == str(direction_id)

        if operator_id is not None:
            mask &= merged["agency_id"] == operator_id

        filtered = merged.loc[mask].copy()

        if filtered.empty:
            return []

        filtered["_time_hhmm"] = filtered["departure_time"].apply(
            self._normalise_time
        )

        filtered.sort_values("_time_hhmm", inplace=True)

        return [
            (
                str(row["trip_id"]),
                row["_time_hhmm"],
                str(row["route_short_name"])
                if pd.notna(row["route_short_name"])
                else None,
            )
            for _, row in filtered.iterrows()
        ]

    async def async_refresh_if_stale(self) -> None:
        """Reload static GTFS data if the cache is absent or older than 24 hours.

        Calls ``async_load`` when ``_loaded_at`` is ``None`` or when more than
        ``STATIC_GTFS_REFRESH_HOURS`` hours have elapsed since the last
        successful load.  Does nothing when the cache is still fresh.
        """
        if self._loaded_at is None or (
            datetime.now() - self._loaded_at
        ) > timedelta(hours=STATIC_GTFS_REFRESH_HOURS):
            await self.async_load()
