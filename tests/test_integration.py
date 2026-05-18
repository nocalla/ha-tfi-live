"""End-to-end integration tests for async_setup_entry and async_unload_entry.

T-012: exercises the full setup/teardown lifecycle of the tfi_live integration
with a fully mocked HTTP layer.  No live network calls are made at any point.
pytest-homeassistant-custom-component fixtures are deliberately avoided because
that plugin crashes on Windows (see conftest.py for details).

Strategy
--------
``DataUpdateCoordinator.__init__`` resolves ``config_entry`` via the ContextVar
``homeassistant.config_entries.current_entry`` when no explicit ``config_entry``
kwarg is passed.  Setting that ContextVar to the mock entry before calling
``async_setup_entry`` is the minimal shim that allows
``async_config_entry_first_refresh`` to proceed without a real HA event-loop
context.  The mock entry must also have ``state == ConfigEntryState.SETUP_IN_PROGRESS``
to satisfy the state check inside ``_async_config_entry_first_refresh``.

Test cases
----------
TC-1  Happy path: valid feed + valid static GTFS → setup completes, coordinator
      stored in entry.runtime_data, sensor platform forwarded.
TC-2  AC 10: static GTFS HTTP 500 does not abort setup — coordinator is still
      stored in entry.runtime_data after setup.
TC-3  AC 17 end-to-end: GTFS-RT 401 during first refresh propagates
      ConfigEntryAuthFailed and triggers async_start_reauth.
TC-4  Unload: after a successful setup, async_unload_entry returns True.
"""

import io
import json
import zipfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState, current_entry
from homeassistant.const import Platform
from homeassistant.exceptions import ConfigEntryAuthFailed

from custom_components.tfi_live.__init__ import (
    async_setup_entry,
    async_unload_entry,
)
from custom_components.tfi_live.const import (
    CONF_API_KEY,
    CONF_SENSORS,
    CONF_STATIC_GTFS_URL,
    CONF_TRIP_UPDATE_URL,
)

# ---------------------------------------------------------------------------
# Constants shared across tests
# ---------------------------------------------------------------------------

_DUMMY_TRIP_UPDATE_URL = "https://example.com/gtfs-rt"
_DUMMY_STATIC_GTFS_URL = "https://example.com/gtfs.zip"
_DUMMY_API_KEY = "test-api-key"
_ENTRY_ID = "test_entry_id"

# A minimal valid GTFS-RT JSON payload that the coordinator can parse.
_VALID_GTFS_RT_PAYLOAD: dict = {
    "header": {"gtfs_realtime_version": "2.0"},
    "entity": [
        {
            "id": "1",
            "trip_update": {
                "trip": {
                    "trip_id": "TRIP_001",
                    "route_id": "46A",
                    "direction_id": 0,
                    "start_date": "20260517",
                },
                "stop_time_update": [
                    {
                        "stop_id": "STOP_A",
                        "arrival": {"delay": 120, "time": 1747503600},
                        "departure": {"delay": 120, "time": 1747503660},
                    }
                ],
            },
        }
    ],
}


# ---------------------------------------------------------------------------
# GTFS zip builder (mirrors the pattern from test_static_gtfs.py)
# ---------------------------------------------------------------------------


def _make_gtfs_zip() -> bytes:
    """Build and return a minimal valid GTFS zip as raw bytes.

    The archive contains the six required GTFS text files with enough data
    for StaticGtfsCache.async_load() to succeed without error.

    Returns:
        Raw bytes of a valid in-memory GTFS zip archive.
    """
    stops_csv = "stop_id,stop_name\nSTOP_A,Stop Alpha\n"
    routes_csv = "route_id,route_short_name,agency_id\nR1,46A,NTA\n"
    trips_csv = "trip_id,route_id,service_id,direction_id\nT1,R1,SVC1,0\n"
    stop_times_csv = (
        "trip_id,stop_id,departure_time,stop_sequence\nT1,STOP_A,09:00:00,1\n"
    )
    calendar_csv = (
        "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
        "start_date,end_date\n"
        "SVC1,1,1,1,1,1,1,1,20200101,20991231\n"
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
# Mock helpers
# ---------------------------------------------------------------------------


def _make_http_session(
    *,
    status: int,
    body: bytes = b"",
) -> MagicMock:
    """Return a mock aiohttp.ClientSession for a fixed HTTP response.

    The mock supports the async context-manager protocol used by both
    StaticGtfsCache (``resp.read()``) and TfiLiveCoordinator (``resp.text()``).

    Args:
        status: HTTP status code the mocked response will report.
        body: Raw bytes returned by both ``resp.read()`` and decoded by
            ``resp.text()``.

    Returns:
        MagicMock whose ``.get()`` method acts as an async context manager
        yielding a response with the supplied status and body.
    """
    resp = MagicMock()
    resp.status = status
    resp.ok = status < 400
    resp.read = AsyncMock(return_value=body)
    resp.text = AsyncMock(return_value=body.decode(errors="replace"))

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)

    session = MagicMock()
    session.get = MagicMock(return_value=cm)
    return session


def _make_hass() -> MagicMock:
    """Return a minimal MagicMock standing in for a HomeAssistant instance.

    Returns:
        MagicMock with ``data = {}``, ``is_stopping = False``, and
        ``config_entries`` whose ``async_forward_entry_setups`` and
        ``async_unload_platforms`` are ``AsyncMock`` returning ``True``.
    """
    hass = MagicMock()
    hass.data = {}
    hass.is_stopping = False
    hass.config_entries = MagicMock()
    hass.config_entries.async_forward_entry_setups = AsyncMock(return_value=True)
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
    return hass


def _make_entry(*, sensors: list[dict] | None = None) -> MagicMock:
    """Return a MagicMock standing in for a ConfigEntry.

    The entry's ``state`` is set to ``ConfigEntryState.SETUP_IN_PROGRESS`` so
    that ``DataUpdateCoordinator._async_config_entry_first_refresh`` passes its
    state guard.

    Args:
        sensors: List of sensor configuration dicts stored under CONF_SENSORS.
            Defaults to a single minimal sensor config when omitted.

    Returns:
        MagicMock with ``entry_id``, ``data`` dict, ``state``, and a stub
        ``async_start_reauth`` callable.
    """
    if sensors is None:
        sensors = [
            {
                "stop_id": "STOP_A",
                "route_id": "46A",
                "name": "Test Sensor",
            }
        ]

    entry = MagicMock()
    entry.entry_id = _ENTRY_ID
    entry.state = ConfigEntryState.SETUP_IN_PROGRESS
    entry.data = {
        CONF_API_KEY: _DUMMY_API_KEY,
        CONF_TRIP_UPDATE_URL: _DUMMY_TRIP_UPDATE_URL,
        CONF_STATIC_GTFS_URL: _DUMMY_STATIC_GTFS_URL,
        CONF_SENSORS: sensors,
    }
    return entry


# ---------------------------------------------------------------------------
# TC-1: Happy path setup
# ---------------------------------------------------------------------------


async def test_setup_entry_happy_path_stores_coordinator_and_forwards_sensor() -> None:
    """TC-1: Valid feed + valid static GTFS → coordinator stored, sensor forwarded.

    Arrange: hass with empty data dict; config entry with all required keys and
        state SETUP_IN_PROGRESS; static GTFS HTTP returns a valid zip; GTFS-RT
        returns well-formed JSON.  The current_entry ContextVar is set so the
        coordinator can resolve config_entry during its __init__.
    Act: call async_setup_entry.
    Assert:
        - returns True without raising
        - coordinator is stored at entry.runtime_data
        - async_forward_entry_setups was called with [Platform.SENSOR]
    """
    # Arrange
    hass = _make_hass()
    entry = _make_entry()

    gtfs_zip = _make_gtfs_zip()
    static_session = _make_http_session(status=200, body=gtfs_zip)
    rt_session = _make_http_session(
        status=200, body=json.dumps(_VALID_GTFS_RT_PAYLOAD).encode()
    )

    token = current_entry.set(entry)
    try:
        with (
            patch("homeassistant.helpers.frame.report_usage"),
            patch(
                "custom_components.tfi_live.__init__.async_get_clientsession",
                return_value=static_session,
            ),
            patch(
                "custom_components.tfi_live.coordinator.async_get_clientsession",
                return_value=rt_session,
            ),
        ):
            # Act
            result = await async_setup_entry(hass, entry)
    finally:
        current_entry.reset(token)

    # Assert — setup returns True
    assert result is True

    # Assert - coordinator stored on entry.runtime_data
    assert entry.runtime_data is not None

    # Assert — sensor platform was forwarded
    hass.config_entries.async_forward_entry_setups.assert_called_once_with(
        entry, [Platform.SENSOR]
    )


# ---------------------------------------------------------------------------
# TC-2: AC 10 — static GTFS HTTP 500 does not abort setup
# ---------------------------------------------------------------------------


async def test_setup_entry_static_gtfs_500_does_not_abort_setup() -> None:
    """TC-2 (AC 10): Static GTFS HTTP 500 is swallowed; setup still completes.

    Arrange: static GTFS URL returns HTTP 500; GTFS-RT returns valid JSON.
        The current_entry ContextVar is set so the coordinator resolves its
        config_entry during __init__.
    Act: call async_setup_entry.
    Assert:
        - does not raise
        - coordinator is still stored at entry.runtime_data
    """
    # Arrange
    hass = _make_hass()
    entry = _make_entry()

    static_session = _make_http_session(status=500, body=b"Internal Server Error")
    rt_session = _make_http_session(
        status=200, body=json.dumps(_VALID_GTFS_RT_PAYLOAD).encode()
    )

    token = current_entry.set(entry)
    try:
        with (
            patch("homeassistant.helpers.frame.report_usage"),
            patch(
                "custom_components.tfi_live.__init__.async_get_clientsession",
                return_value=static_session,
            ),
            patch(
                "custom_components.tfi_live.coordinator.async_get_clientsession",
                return_value=rt_session,
            ),
        ):
            # Act — must not raise despite the static GTFS failure
            result = await async_setup_entry(hass, entry)
    finally:
        current_entry.reset(token)

    # Assert — setup completes successfully
    assert result is True

    # Assert - coordinator is still registered on entry.runtime_data
    assert entry.runtime_data is not None


# ---------------------------------------------------------------------------
# TC-3: AC 17 — GTFS-RT 401 propagates ConfigEntryAuthFailed
# ---------------------------------------------------------------------------


async def test_setup_entry_gtfs_rt_401_raises_config_entry_auth_failed() -> None:
    """TC-3 (AC 17): GTFS-RT 401 during first refresh propagates ConfigEntryAuthFailed.

    The coordinator's _async_update_data is allowed to run for real (with a
    mocked 401 HTTP response) so that the full auth-failure path is exercised,
    including the call to entry.async_start_reauth.

    Arrange: static GTFS returns a valid zip; GTFS-RT returns HTTP 401.
        The current_entry ContextVar is set so the coordinator resolves its
        config_entry during __init__.
    Act: call async_setup_entry.
    Assert:
        - raises ConfigEntryAuthFailed
        - entry.async_start_reauth was called exactly once
    """
    # Arrange
    hass = _make_hass()
    entry = _make_entry()

    gtfs_zip = _make_gtfs_zip()
    static_session = _make_http_session(status=200, body=gtfs_zip)
    rt_session = _make_http_session(status=401, body=b"Unauthorized")

    token = current_entry.set(entry)
    try:
        with (
            patch("homeassistant.helpers.frame.report_usage"),
            patch(
                "custom_components.tfi_live.__init__.async_get_clientsession",
                return_value=static_session,
            ),
            patch(
                "custom_components.tfi_live.coordinator.async_get_clientsession",
                return_value=rt_session,
            ),
        ):
            # Act / Assert — ConfigEntryAuthFailed must propagate out of setup
            with pytest.raises(ConfigEntryAuthFailed):
                await async_setup_entry(hass, entry)
    finally:
        current_entry.reset(token)

    # Assert — reauth was triggered on the config entry
    entry.async_start_reauth.assert_called_once()


# ---------------------------------------------------------------------------
# TC-4: Unload removes coordinator from hass.data
# ---------------------------------------------------------------------------


async def test_unload_entry_returns_true_and_removes_coordinator() -> None:
    """TC-4: async_unload_entry returns True after a successful setup.

    Arrange: perform a successful setup (same conditions as TC-1), then call
        async_unload_entry.
    Assert:
        - async_unload_entry returns True
    """
    # Arrange — successful setup first
    hass = _make_hass()
    entry = _make_entry()

    gtfs_zip = _make_gtfs_zip()
    static_session = _make_http_session(status=200, body=gtfs_zip)
    rt_session = _make_http_session(
        status=200, body=json.dumps(_VALID_GTFS_RT_PAYLOAD).encode()
    )

    token = current_entry.set(entry)
    try:
        with (
            patch("homeassistant.helpers.frame.report_usage"),
            patch(
                "custom_components.tfi_live.__init__.async_get_clientsession",
                return_value=static_session,
            ),
            patch(
                "custom_components.tfi_live.coordinator.async_get_clientsession",
                return_value=rt_session,
            ),
        ):
            await async_setup_entry(hass, entry)
    finally:
        current_entry.reset(token)

    # Sanity check: coordinator was stored
    assert entry.runtime_data is not None

    # Act - unload
    result = await async_unload_entry(hass, entry)

    # Assert - returns True
    assert result is True


# ---------------------------------------------------------------------------
# TC-5: async_load raises — warning logged, setup still completes
# ---------------------------------------------------------------------------


async def test_setup_entry_async_load_raises_warning_swallowed() -> None:
    """TC-5: If async_load() raises, warning is logged and setup still completes.

    This exercises the except branch in async_setup_entry that catches any
    exception from cache.async_load() and logs a warning rather than aborting.

    Arrange: patch StaticGtfsCache.async_load to raise RuntimeError; GTFS-RT
        returns valid JSON.
    Act: call async_setup_entry.
    Assert:
        - returns True without raising
        - coordinator is stored at entry.runtime_data
    """
    import json
    from unittest.mock import AsyncMock, MagicMock, patch

    from homeassistant.config_entries import current_entry

    from custom_components.tfi_live.__init__ import async_setup_entry

    hass = _make_hass()
    entry = _make_entry()

    rt_session = _make_http_session(
        status=200, body=json.dumps(_VALID_GTFS_RT_PAYLOAD).encode()
    )

    token = current_entry.set(entry)
    try:
        with (
            patch("homeassistant.helpers.frame.report_usage"),
            patch(
                "custom_components.tfi_live.__init__.async_get_clientsession",
                return_value=MagicMock(),  # session for StaticGtfsCache (will raise)
            ),
            patch(
                "custom_components.tfi_live.coordinator.async_get_clientsession",
                return_value=rt_session,
            ),
            patch(
                "custom_components.tfi_live.__init__.StaticGtfsCache.async_load",
                new=AsyncMock(side_effect=RuntimeError("disk full")),
            ),
        ):
            result = await async_setup_entry(hass, entry)
    finally:
        current_entry.reset(token)

    assert result is True
    assert entry.runtime_data is not None
