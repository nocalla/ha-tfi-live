"""Tests for custom_components.ha_tfi_live.static_gtfs.

Covers StaticGtfsCache.async_load(), get_scheduled_departures(), and
async_refresh_if_stale() with fully mocked aiohttp sessions and synthetic
in-memory GTFS zips. No live network calls are made at any point.
"""

import io
import zipfile
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from custom_components.ha_tfi_live.static_gtfs import StaticGtfsCache

# ---------------------------------------------------------------------------
# Synthetic GTFS data constants
# ---------------------------------------------------------------------------

_STOP_A = "STOP_A"
_STOP_B = "STOP_B"
_ROUTE_SHORT_46A = "46A"
_ROUTE_SHORT_39A = "39A"
_AGENCY_NTA = "NTA"

# Monday 2026-05-18; used across tests that need a weekday
_MONDAY = date(2026, 5, 18)

# A default dummy URL — the tests mock the HTTP layer so the value does not
# matter, but it must be a non-empty string.
_DUMMY_URL = "https://example.com/gtfs.zip"


# ---------------------------------------------------------------------------
# Synthetic GTFS zip builder
# ---------------------------------------------------------------------------


def make_gtfs_zip(
    *,
    stop_a_id: str = _STOP_A,
    stop_b_id: str = _STOP_B,
    route_46a_id: str = "R1",
    route_39a_id: str = "R2",
    trip_times: list[tuple[str, str, str]] | None = None,
    service_id: str = "SVC1",
) -> bytes:
    """Build a minimal in-memory GTFS zip and return the raw bytes.

    The zip contains stops.txt, routes.txt, trips.txt, stop_times.txt,
    calendar.txt, and calendar_dates.txt (empty header only).

    Args:
        stop_a_id: GTFS stop_id for the first stop.
        stop_b_id: GTFS stop_id for the second stop.
        route_46a_id: route_id for the 46A route.
        route_39a_id: route_id for the 39A route.
        trip_times: List of (trip_id, stop_id, departure_time) tuples used to
            populate stop_times.txt. Defaults to a small set covering both
            stops with several departure times so sort-order tests are
            non-trivial.
        service_id: GTFS service_id to assign to all trips and the calendar
            row.

    Returns:
        Raw bytes of the assembled GTFS zip archive.
    """
    stops_csv = f"stop_id,stop_name\n{stop_a_id},Stop Alpha\n{stop_b_id},Stop Beta\n"

    routes_csv = (
        "route_id,route_short_name,agency_id\n"
        f"{route_46a_id},{_ROUTE_SHORT_46A},{_AGENCY_NTA}\n"
        f"{route_39a_id},{_ROUTE_SHORT_39A},{_AGENCY_NTA}\n"
    )

    trips_csv = (
        "trip_id,route_id,service_id,direction_id\n"
        f"T1_{route_46a_id},{route_46a_id},{service_id},0\n"
        f"T2_{route_46a_id},{route_46a_id},{service_id},1\n"
        f"T3_{route_46a_id},{route_46a_id},{service_id},0\n"
        f"T4_{route_39a_id},{route_39a_id},{service_id},0\n"
    )

    if trip_times is None:
        # Deliberately out of order to validate sort behaviour (TC-3).
        trip_times = [
            (f"T1_{route_46a_id}", stop_a_id, "09:00:00"),
            (f"T1_{route_46a_id}", stop_b_id, "09:15:00"),
            (f"T2_{route_46a_id}", stop_a_id, "08:00:00"),
            (f"T2_{route_46a_id}", stop_b_id, "08:20:00"),
            (f"T3_{route_46a_id}", stop_a_id, "10:00:00"),
            (f"T3_{route_46a_id}", stop_b_id, "10:20:00"),
            (f"T4_{route_39a_id}", stop_a_id, "07:30:00"),
        ]

    stop_times_rows = "\n".join(
        f"{trip},{stop},{dep},{seq}"
        for seq, (trip, stop, dep) in enumerate(trip_times, start=1)
    )
    stop_times_csv = "trip_id,stop_id,departure_time,stop_sequence\n" + stop_times_rows

    # All days active, extremely wide date range.
    calendar_csv = (
        "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
        "start_date,end_date\n"
        f"{service_id},1,1,1,1,1,1,1,20200101,20991231\n"
    )

    calendar_dates_csv = "service_id,date,exception_type\n"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("stops.txt", stops_csv)
        zf.writestr("routes.txt", routes_csv)
        zf.writestr("trips.txt", trips_csv)
        zf.writestr("stop_times.txt", stop_times_csv)
        zf.writestr("calendar.txt", calendar_csv)
        zf.writestr("calendar_dates.txt", calendar_dates_csv)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Mock-session helper
# ---------------------------------------------------------------------------


def _make_session(*, status: int = 200, body: bytes = b"") -> MagicMock:
    """Return a MagicMock that quacks like an aiohttp.ClientSession.

    Args:
        status: HTTP status code the mocked response will report.
        body: Bytes returned by ``response.read()``.

    Returns:
        MagicMock whose ``.get(url)`` is an async context manager yielding a
        mock response with the given status and body.
    """
    mock_response = MagicMock()
    mock_response.ok = status < 400
    mock_response.status = status
    mock_response.read = AsyncMock(return_value=body)

    mock_session = MagicMock()
    mock_session.get = MagicMock(
        return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_response),
            __aexit__=AsyncMock(return_value=False),
        )
    )
    return mock_session


# ---------------------------------------------------------------------------
# TC-1 — successful load sets available and _loaded_at
# ---------------------------------------------------------------------------


async def test_async_load_success_sets_available_and_loaded_at() -> None:
    """async_load() with a 200 response sets available=True and _loaded_at."""
    zip_bytes = make_gtfs_zip()
    session = _make_session(status=200, body=zip_bytes)
    cache = StaticGtfsCache(_DUMMY_URL, session)

    assert cache.available is False
    assert cache._loaded_at is None

    await cache.async_load()

    assert cache.available is True
    assert cache._loaded_at is not None


# ---------------------------------------------------------------------------
# TC-2 — get_scheduled_departures returns correct typed tuples
# ---------------------------------------------------------------------------


async def test_get_scheduled_departures_returns_correct_trips() -> None:
    """get_scheduled_departures() returns non-empty list of correctly typed
    tuples, all with the expected route_short_name, after a successful load."""
    zip_bytes = make_gtfs_zip()
    session = _make_session(status=200, body=zip_bytes)
    cache = StaticGtfsCache(_DUMMY_URL, session)
    await cache.async_load()

    results = cache.get_scheduled_departures(
        _STOP_A, _ROUTE_SHORT_46A, None, None, _MONDAY
    )

    assert len(results) > 0
    for trip_id, time_hhmm, route_name in results:
        assert isinstance(trip_id, str)
        assert isinstance(time_hhmm, str)
        # HH:MM format
        assert len(time_hhmm) == 5
        assert time_hhmm[2] == ":"
        assert route_name == _ROUTE_SHORT_46A


# ---------------------------------------------------------------------------
# TC-3 — get_scheduled_departures returns results sorted ascending
# ---------------------------------------------------------------------------


async def test_get_scheduled_departures_sorted_ascending() -> None:
    """Departure times in the result are in ascending HH:MM order."""
    # Provide stop_times deliberately out of time order (09:00, 08:00, 10:00)
    # so that the sort is non-trivial.
    route_46a_id = "R1"
    trip_times = [
        (f"T1_{route_46a_id}", _STOP_A, "09:00:00"),
        (f"T2_{route_46a_id}", _STOP_A, "08:00:00"),
        (f"T3_{route_46a_id}", _STOP_A, "10:00:00"),
    ]
    zip_bytes = make_gtfs_zip(route_46a_id=route_46a_id, trip_times=trip_times)
    session = _make_session(status=200, body=zip_bytes)
    cache = StaticGtfsCache(_DUMMY_URL, session)
    await cache.async_load()

    results = cache.get_scheduled_departures(
        _STOP_A, _ROUTE_SHORT_46A, None, None, _MONDAY
    )

    times = [r[1] for r in results]
    assert times == sorted(times), f"Times not sorted ascending: {times}"
    assert times == ["08:00", "09:00", "10:00"]


# ---------------------------------------------------------------------------
# TC-4 — get_scheduled_departures returns [] when unavailable
# ---------------------------------------------------------------------------


async def test_get_scheduled_departures_returns_empty_when_unavailable() -> None:
    """get_scheduled_departures() returns [] when cache has never been loaded."""
    session = _make_session(status=200, body=b"irrelevant")
    cache = StaticGtfsCache(_DUMMY_URL, session)
    # Do NOT call async_load — cache.available stays False.

    results = cache.get_scheduled_departures(
        _STOP_A, _ROUTE_SHORT_46A, None, None, _MONDAY
    )

    assert results == []


# ---------------------------------------------------------------------------
# TC-5 — HTTP error sets available=False
# ---------------------------------------------------------------------------


async def test_async_load_http_error_sets_unavailable() -> None:
    """async_load() with a 503 response sets available=False."""
    session = _make_session(status=503, body=b"Service Unavailable")
    cache = StaticGtfsCache(_DUMMY_URL, session)

    await cache.async_load()

    assert cache.available is False


# ---------------------------------------------------------------------------
# TC-6 — HTTP error logs exactly one WARNING across two consecutive calls
# ---------------------------------------------------------------------------


async def test_async_load_http_error_logs_once_not_twice(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two consecutive async_load() calls with a 503 emit exactly one WARNING."""
    session = _make_session(status=503, body=b"Service Unavailable")
    cache = StaticGtfsCache(_DUMMY_URL, session)

    import logging

    with caplog.at_level(
        logging.WARNING, logger="custom_components.ha_tfi_live.static_gtfs"
    ):
        await cache.async_load()
        await cache.async_load()

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, (
        f"Expected exactly 1 WARNING, got {len(warnings)}: "
        + str([r.message for r in warnings])
    )


# ---------------------------------------------------------------------------
# TC-7 — parse error (non-zip bytes) sets available=False
# ---------------------------------------------------------------------------


async def test_async_load_parse_error_sets_unavailable() -> None:
    """async_load() with a 200 response but non-zip body sets available=False."""
    session = _make_session(status=200, body=b"not a zip")
    cache = StaticGtfsCache(_DUMMY_URL, session)

    await cache.async_load()

    assert cache.available is False


# ---------------------------------------------------------------------------
# TC-8 — async_refresh_if_stale does not reload when fresh (< 24 h)
# ---------------------------------------------------------------------------


async def test_async_load_client_error_sets_unavailable() -> None:
    """async_load() when session.get() raises ClientError sets available=False."""
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(side_effect=aiohttp.ClientError("connection refused"))
    cm.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.get = MagicMock(return_value=cm)
    cache = StaticGtfsCache(_DUMMY_URL, session)

    await cache.async_load()

    assert cache.available is False
    assert cache._in_error_state is True


async def test_async_load_client_error_logs_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """async_load() when session.get() raises aiohttp.ClientError emits a WARNING."""
    import logging

    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(side_effect=aiohttp.ClientError("timeout"))
    cm.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.get = MagicMock(return_value=cm)
    cache = StaticGtfsCache(_DUMMY_URL, session)

    with caplog.at_level(
        logging.WARNING, logger="custom_components.ha_tfi_live.static_gtfs"
    ):
        await cache.async_load()

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1


async def test_async_refresh_if_stale_no_reload_when_fresh() -> None:
    """async_refresh_if_stale() is a no-op when _loaded_at is 1 hour ago."""
    zip_bytes = make_gtfs_zip()
    session = _make_session(status=200, body=zip_bytes)
    cache = StaticGtfsCache(_DUMMY_URL, session)
    await cache.async_load()

    # Patch async_load so we can count subsequent calls.
    with patch.object(cache, "async_load", new_callable=AsyncMock) as mock_load:
        cache._loaded_at = datetime.now(UTC) - timedelta(hours=1)
        await cache.async_refresh_if_stale()

    mock_load.assert_not_called()


# ---------------------------------------------------------------------------
# TC-9 — async_refresh_if_stale reloads when stale (> 24 h)
# ---------------------------------------------------------------------------


async def test_async_refresh_if_stale_reloads_when_stale() -> None:
    """async_refresh_if_stale() calls async_load() when _loaded_at is 25 h ago."""
    zip_bytes = make_gtfs_zip()
    session = _make_session(status=200, body=zip_bytes)
    cache = StaticGtfsCache(_DUMMY_URL, session)
    await cache.async_load()

    with patch.object(cache, "async_load", new_callable=AsyncMock) as mock_load:
        cache._loaded_at = datetime.now(UTC) - timedelta(hours=25)
        await cache.async_refresh_if_stale()

    mock_load.assert_called_once()


# ---------------------------------------------------------------------------
# TC-10 — successful reload after error resets error state
# ---------------------------------------------------------------------------


async def test_async_load_success_resets_error_state() -> None:
    """A 200 response following a 503 sets available=True and _in_error_state=False."""
    # First load: HTTP 503 → error state
    session_err = _make_session(status=503, body=b"")
    cache = StaticGtfsCache(_DUMMY_URL, session_err)
    await cache.async_load()

    assert cache.available is False
    assert cache._in_error_state is True

    # Second load: HTTP 200 with valid zip → should recover
    zip_bytes = make_gtfs_zip()
    session_ok = _make_session(status=200, body=zip_bytes)
    cache._session = session_ok
    await cache.async_load()

    assert cache.available is True
    assert cache._in_error_state is False


# ---------------------------------------------------------------------------
# Additional boundary / coverage tests
# ---------------------------------------------------------------------------


async def test_get_scheduled_departures_filters_by_direction_id() -> None:
    """direction_id filter restricts results to matching trips only."""
    zip_bytes = make_gtfs_zip()
    session = _make_session(status=200, body=zip_bytes)
    cache = StaticGtfsCache(_DUMMY_URL, session)
    await cache.async_load()

    results_dir0 = cache.get_scheduled_departures(
        _STOP_A, _ROUTE_SHORT_46A, 0, None, _MONDAY
    )
    results_dir1 = cache.get_scheduled_departures(
        _STOP_A, _ROUTE_SHORT_46A, 1, None, _MONDAY
    )

    # Direction 0 and direction 1 both have matching trips in the synthetic data;
    # the union must equal the unfiltered result.
    results_all = cache.get_scheduled_departures(
        _STOP_A, _ROUTE_SHORT_46A, None, None, _MONDAY
    )
    trip_ids_dir0 = {r[0] for r in results_dir0}
    trip_ids_dir1 = {r[0] for r in results_dir1}
    trip_ids_all = {r[0] for r in results_all}

    assert trip_ids_dir0 | trip_ids_dir1 == trip_ids_all
    assert trip_ids_dir0.isdisjoint(trip_ids_dir1)


async def test_get_scheduled_departures_filters_by_operator_id() -> None:
    """operator_id filter restricts results to the matching agency only."""
    zip_bytes = make_gtfs_zip()
    session = _make_session(status=200, body=zip_bytes)
    cache = StaticGtfsCache(_DUMMY_URL, session)
    await cache.async_load()

    results_nta = cache.get_scheduled_departures(
        _STOP_A, _ROUTE_SHORT_46A, None, _AGENCY_NTA, _MONDAY
    )
    results_other = cache.get_scheduled_departures(
        _STOP_A, _ROUTE_SHORT_46A, None, "OTHER_AGENCY", _MONDAY
    )

    assert len(results_nta) > 0
    assert results_other == []


async def test_get_scheduled_departures_no_match_for_unknown_stop() -> None:
    """get_scheduled_departures() returns [] for a stop_id not in the data."""
    zip_bytes = make_gtfs_zip()
    session = _make_session(status=200, body=zip_bytes)
    cache = StaticGtfsCache(_DUMMY_URL, session)
    await cache.async_load()

    results = cache.get_scheduled_departures(
        "STOP_UNKNOWN", _ROUTE_SHORT_46A, None, None, _MONDAY
    )

    assert results == []


async def test_get_scheduled_departures_no_match_for_unknown_route() -> None:
    """get_scheduled_departures() returns [] for a route_short_name not in data."""
    zip_bytes = make_gtfs_zip()
    session = _make_session(status=200, body=zip_bytes)
    cache = StaticGtfsCache(_DUMMY_URL, session)
    await cache.async_load()

    results = cache.get_scheduled_departures(_STOP_A, "999X", None, None, _MONDAY)

    assert results == []


async def test_async_refresh_if_stale_reloads_when_loaded_at_is_none() -> None:
    """async_refresh_if_stale() calls async_load() when _loaded_at is None."""
    session = _make_session(status=200, body=b"")
    cache = StaticGtfsCache(_DUMMY_URL, session)
    # _loaded_at starts as None

    with patch.object(cache, "async_load", new_callable=AsyncMock) as mock_load:
        await cache.async_refresh_if_stale()

    mock_load.assert_called_once()


async def test_normalise_time_wraps_hours_past_midnight() -> None:
    """_normalise_time wraps GTFS hours >= 24 modulo 24."""
    assert StaticGtfsCache._normalise_time("25:30:00") == "01:30"
    assert StaticGtfsCache._normalise_time("24:00:00") == "00:00"
    assert StaticGtfsCache._normalise_time("08:05:00") == "08:05"


async def test_async_load_parse_error_logs_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """async_load() with a non-zip body emits a WARNING log."""
    import logging

    session = _make_session(status=200, body=b"not a zip")
    cache = StaticGtfsCache(_DUMMY_URL, session)

    with caplog.at_level(
        logging.WARNING, logger="custom_components.ha_tfi_live.static_gtfs"
    ):
        await cache.async_load()

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1


async def test_async_load_parse_error_logs_once_not_twice(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two consecutive parse-failure loads emit exactly one WARNING."""
    import logging

    session = _make_session(status=200, body=b"not a zip")
    cache = StaticGtfsCache(_DUMMY_URL, session)

    with caplog.at_level(
        logging.WARNING, logger="custom_components.ha_tfi_live.static_gtfs"
    ):
        await cache.async_load()
        await cache.async_load()

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1


async def test_calendar_dates_exception_type_1_adds_service() -> None:
    """A calendar_dates entry with exception_type=1 adds a service for that date."""
    # Build a zip where the calendar has a service NOT active on a Sunday (day 6)
    # but calendar_dates adds it back via exception_type=1.
    target = date(2026, 5, 17)  # Sunday
    service_id = "WEEKDAY_ONLY"

    stops_csv = f"stop_id,stop_name\n{_STOP_A},Alpha\n"
    routes_csv = (
        f"route_id,route_short_name,agency_id\nR1,{_ROUTE_SHORT_46A},{_AGENCY_NTA}\n"
    )
    trips_csv = f"trip_id,route_id,service_id,direction_id\nT1,R1,{service_id},0\n"
    stop_times_csv = (
        f"trip_id,stop_id,departure_time,stop_sequence\nT1,{_STOP_A},09:00:00,1\n"
    )
    # Monday–Saturday only (sunday=0)
    calendar_csv = (
        "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
        "start_date,end_date\n"
        f"{service_id},1,1,1,1,1,1,0,20200101,20991231\n"
    )
    # Exception: service added on this specific Sunday
    calendar_dates_csv = f"service_id,date,exception_type\n{service_id},20260517,1\n"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w") as zf:
        zf.writestr("stops.txt", stops_csv)
        zf.writestr("routes.txt", routes_csv)
        zf.writestr("trips.txt", trips_csv)
        zf.writestr("stop_times.txt", stop_times_csv)
        zf.writestr("calendar.txt", calendar_csv)
        zf.writestr("calendar_dates.txt", calendar_dates_csv)
    zip_bytes = buf.getvalue()

    session = _make_session(status=200, body=zip_bytes)
    cache = StaticGtfsCache(_DUMMY_URL, session)
    await cache.async_load()

    results = cache.get_scheduled_departures(
        _STOP_A, _ROUTE_SHORT_46A, None, None, target
    )
    assert len(results) == 1


async def test_calendar_dates_exception_type_2_removes_service() -> None:
    """exception_type=2 in calendar_dates removes a normally-active service."""
    target = _MONDAY  # Monday — normally active in the calendar
    service_id = "SVC1"

    stops_csv = f"stop_id,stop_name\n{_STOP_A},Alpha\n"
    routes_csv = (
        f"route_id,route_short_name,agency_id\nR1,{_ROUTE_SHORT_46A},{_AGENCY_NTA}\n"
    )
    trips_csv = "trip_id,route_id,service_id,direction_id\nT1,R1,SVC1,0\n"
    stop_times_csv = (
        f"trip_id,stop_id,departure_time,stop_sequence\nT1,{_STOP_A},09:00:00,1\n"
    )
    calendar_csv = (
        "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
        "start_date,end_date\n"
        f"{service_id},1,1,1,1,1,1,1,20200101,20991231\n"
    )
    # Remove the service on this specific Monday
    calendar_dates_csv = f"service_id,date,exception_type\n{service_id},20260518,2\n"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w") as zf:
        zf.writestr("stops.txt", stops_csv)
        zf.writestr("routes.txt", routes_csv)
        zf.writestr("trips.txt", trips_csv)
        zf.writestr("stop_times.txt", stop_times_csv)
        zf.writestr("calendar.txt", calendar_csv)
        zf.writestr("calendar_dates.txt", calendar_dates_csv)
    zip_bytes = buf.getvalue()

    session = _make_session(status=200, body=zip_bytes)
    cache = StaticGtfsCache(_DUMMY_URL, session)
    await cache.async_load()

    results = cache.get_scheduled_departures(
        _STOP_A, _ROUTE_SHORT_46A, None, None, target
    )
    assert results == []


async def test_zip_without_calendar_dates_file_uses_empty_dataframe() -> None:
    """A GTFS zip missing calendar_dates.txt does not error and uses an empty DF."""
    stops_csv = f"stop_id,stop_name\n{_STOP_A},Alpha\n"
    routes_csv = (
        f"route_id,route_short_name,agency_id\nR1,{_ROUTE_SHORT_46A},{_AGENCY_NTA}\n"
    )
    trips_csv = "trip_id,route_id,service_id,direction_id\nT1,R1,SVC1,0\n"
    stop_times_csv = (
        f"trip_id,stop_id,departure_time,stop_sequence\nT1,{_STOP_A},09:00:00,1\n"
    )
    calendar_csv = (
        "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
        "start_date,end_date\n"
        "SVC1,1,1,1,1,1,1,1,20200101,20991231\n"
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w") as zf:
        zf.writestr("stops.txt", stops_csv)
        zf.writestr("routes.txt", routes_csv)
        zf.writestr("trips.txt", trips_csv)
        zf.writestr("stop_times.txt", stop_times_csv)
        zf.writestr("calendar.txt", calendar_csv)
        # calendar_dates.txt deliberately omitted
    zip_bytes = buf.getvalue()

    session = _make_session(status=200, body=zip_bytes)
    cache = StaticGtfsCache(_DUMMY_URL, session)
    await cache.async_load()

    assert cache.available is True
    results = cache.get_scheduled_departures(
        _STOP_A, _ROUTE_SHORT_46A, None, None, _MONDAY
    )
    assert len(results) == 1
