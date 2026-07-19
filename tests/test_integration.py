"""End-to-end integration tests for async_setup_entry and async_unload_entry.

T-012: exercises the full setup/teardown lifecycle of the tfi_live integration
with mocked library clients.  No live network calls are made at any point.
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

Library clients (``GtfsRtClient`` and ``StaticGtfsClient``) are patched at the
class level so that instances created inside ``async_setup_entry`` use mocked
methods without requiring real HTTP sessions.

Test cases
----------
TC-1  Happy path: static GTFS load succeeds + valid RT feed → setup completes,
      coordinator stored in entry.runtime_data, sensor platform forwarded.
TC-2  Static GTFS load is scheduled as a background task, never awaited
      inline during setup; running the task performs the load.
TC-3  StaticGtfsLoadError in the background load logs a WARNING and is
      swallowed — the entry stays set up.
TC-4  GtfsRtAuthError during first refresh propagates
      ConfigEntryAuthFailed and triggers async_start_reauth.
TC-5  Unload: after a successful setup, async_unload_entry returns True.

Issue #100: setup passes the configured sensors' stop IDs (deduplicated) to
StaticGtfsClient as ``stop_ids`` so the static parse only indexes those stops.
"""

import logging
from contextlib import contextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState, current_entry
from homeassistant.const import Platform
from homeassistant.core import CoreState
from homeassistant.exceptions import ConfigEntryAuthFailed
from nta_gtfs import (
    GtfsRtAuthError,
    GtfsRtClient,
    StaticGtfsClient,
    StaticGtfsLoadError,
    StopTimeUpdate,
    TripUpdate,
)

from custom_components.tfi_live.__init__ import (
    async_migrate_entry,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.tfi_live.const import (
    CONF_API_KEY,
    CONF_SENSORS,
    CONF_STATIC_GTFS_URL,
    CONF_TRIP_UPDATE_URL,
)
from custom_components.tfi_live.sensor import TfiLiveSensor
from custom_components.tfi_live.sensor import (
    async_setup_entry as sensor_async_setup_entry,
)

# ---------------------------------------------------------------------------
# Constants shared across tests
# ---------------------------------------------------------------------------

_DUMMY_TRIP_UPDATE_URL = "https://example.com/gtfs-rt"
_DUMMY_STATIC_GTFS_URL = "https://example.com/gtfs.zip"
_DUMMY_API_KEY = "test-api-key"
_ENTRY_ID = "test_entry_id"

_VALID_TRIP_UPDATE = TripUpdate(
    trip_id="TRIP_001",
    route_id="46A",
    direction_id="0",
    start_date="20260517",
    stop_time_updates=[
        StopTimeUpdate(
            stop_id="STOP_A",
            arrival_delay=120,
            departure_delay=120,
            arrival_time=1747503600,
            departure_time=1747503660,
        )
    ],
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_hass() -> MagicMock:
    """Return a minimal MagicMock standing in for a HomeAssistant instance.

    Returns:
        MagicMock with ``data = {}``, ``is_stopping = False``, ``state =
        CoreState.running`` (an entry set up after HA startup, so the static
        GTFS load is not deferred), and ``config_entries`` whose
        ``async_forward_entry_setups`` and ``async_unload_platforms`` are
        ``AsyncMock`` returning ``True``.
    """
    hass = MagicMock()
    hass.data = {}
    hass.is_stopping = False
    hass.state = CoreState.running
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
        sensors: List of sensor configuration dicts stored under
            ``entry.options[CONF_SENSORS]``. Defaults to a single minimal
            sensor config when omitted.

    Returns:
        MagicMock with ``entry_id``, ``data`` dict, ``options`` dict,
        ``state``, and a stub ``async_start_reauth`` callable.
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
    }
    entry.options = {
        CONF_SENSORS: sensors,
    }
    # Capture background coroutines scheduled by the coordinator (the static
    # GTFS load) on entry.background_coros so tests can run or discard them.
    entry.background_coros = []

    def _capture_background_task(hass, coro, name=None):
        entry.background_coros.append(coro)
        return MagicMock()

    entry.async_create_background_task = MagicMock(side_effect=_capture_background_task)
    return entry


@contextmanager
def _base_patches(
    *,
    static_load_side_effect=None,
    rt_return_value=None,
    rt_side_effect=None,
):
    """Context manager that applies the standard integration test patches.

    Args:
        static_load_side_effect: If set, ``StaticGtfsClient.async_load`` will
            raise this exception instead of returning normally.
        rt_return_value: List of ``TripUpdate`` objects returned by
            ``GtfsRtClient.async_fetch_trip_updates``.  Defaults to a single
            valid update when ``None`` and ``rt_side_effect`` is also ``None``.
        rt_side_effect: If set, ``GtfsRtClient.async_fetch_trip_updates`` will
            raise this exception.  Takes precedence over ``rt_return_value``.

    Yields:
        Dict with the ``"static"`` and ``"rt"`` AsyncMocks so callers can
        assert on await counts.
    """
    if rt_return_value is None and rt_side_effect is None:
        rt_return_value = [_VALID_TRIP_UPDATE]

    static_mock = AsyncMock(side_effect=static_load_side_effect)
    rt_mock = AsyncMock(return_value=rt_return_value, side_effect=rt_side_effect)

    with (
        patch("homeassistant.helpers.frame.report_usage"),
        patch(
            "custom_components.tfi_live.__init__.async_get_clientsession",
            return_value=MagicMock(),
        ),
        patch.object(StaticGtfsClient, "async_load", static_mock),
        patch.object(GtfsRtClient, "async_fetch_trip_updates", rt_mock),
    ):
        yield {"static": static_mock, "rt": rt_mock}


# ---------------------------------------------------------------------------
# TC-1: Happy path setup
# ---------------------------------------------------------------------------


async def test_setup_entry_happy_path_stores_coordinator_and_forwards_sensor() -> None:
    """TC-1: Valid RT feed + static load success → coordinator stored, sensor forwarded.

    Arrange: hass with empty data dict; config entry with all required keys and
        state SETUP_IN_PROGRESS; StaticGtfsClient.async_load succeeds;
        GtfsRtClient.async_fetch_trip_updates returns a valid list.
    Act: call async_setup_entry.
    Assert:
        - returns True without raising
        - coordinator is stored at entry.runtime_data
        - async_forward_entry_setups was called with [Platform.SENSOR]
    """
    hass = _make_hass()
    entry = _make_entry()

    token = current_entry.set(entry)
    try:
        with _base_patches():
            result = await async_setup_entry(hass, entry)
    finally:
        current_entry.reset(token)
        for coro in entry.background_coros:
            coro.close()

    assert result is True
    assert entry.runtime_data is not None
    hass.config_entries.async_forward_entry_setups.assert_called_once_with(
        entry, [Platform.SENSOR]
    )


# ---------------------------------------------------------------------------
# Issue #100: configured stop IDs are passed to the static GTFS client
# ---------------------------------------------------------------------------


async def test_setup_entry_passes_configured_stop_ids_to_static_client() -> None:
    """Issue #100: setup passes the configured stop IDs to StaticGtfsClient.

    Restricting the static GTFS parse to the configured stops is what keeps
    peak memory low on nationwide feeds, so the integration must forward
    every configured sensor's stop ID — deduplicated — as ``stop_ids``.

    Arrange: entry with three sensors across two distinct stops;
        StaticGtfsClient replaced by a MagicMock class in the integration
        module namespace to capture constructor kwargs.
    Act: call async_setup_entry.
    Assert:
        - setup returns True
        - StaticGtfsClient was constructed with stop_ids == the set of
          distinct configured stop IDs
    """
    hass = _make_hass()
    entry = _make_entry(
        sensors=[
            {"stop_id": "STOP_A", "route_id": "46A", "name": "Sensor A"},
            {"stop_id": "STOP_B", "route_id": "145", "name": "Sensor B"},
            {"stop_id": "STOP_A", "route_id": "155", "name": "Sensor A2"},
        ]
    )

    static_client_cls = MagicMock()
    static_client_cls.return_value = MagicMock(loaded_at=None)

    token = current_entry.set(entry)
    try:
        with (
            _base_patches(),
            patch(
                "custom_components.tfi_live.__init__.StaticGtfsClient",
                static_client_cls,
            ),
        ):
            result = await async_setup_entry(hass, entry)
    finally:
        current_entry.reset(token)
        for coro in entry.background_coros:
            coro.close()

    assert result is True
    static_client_cls.assert_called_once()
    assert static_client_cls.call_args.kwargs["stop_ids"] == {"STOP_A", "STOP_B"}


# ---------------------------------------------------------------------------
# TC-2: static GTFS load runs in a background task, not inline
# ---------------------------------------------------------------------------


async def test_setup_entry_schedules_static_load_in_background() -> None:
    """TC-2: Setup schedules the static GTFS load instead of awaiting it.

    Arrange: hass and entry as in TC-1.
    Act: call async_setup_entry, then run the captured background coroutine.
    Assert:
        - setup returns True with async_load never awaited inline
        - exactly one background task was scheduled
        - running the scheduled coroutine performs the static load
    """
    hass = _make_hass()
    entry = _make_entry()

    token = current_entry.set(entry)
    try:
        with _base_patches() as mocks:
            result = await async_setup_entry(hass, entry)

            assert result is True
            assert mocks["static"].await_count == 0
            assert entry.async_create_background_task.call_count == 1

            await entry.background_coros[0]
            assert mocks["static"].await_count == 1
    finally:
        current_entry.reset(token)


# ---------------------------------------------------------------------------
# TC-3: StaticGtfsLoadError in the background load logs a WARNING
# ---------------------------------------------------------------------------


async def test_background_static_load_error_logs_warning(caplog) -> None:
    """TC-3: StaticGtfsLoadError in the background task logs a WARNING.

    The failure must be swallowed by the background task — the entry stays
    set up and running on real-time data alone.
    """
    hass = _make_hass()
    entry = _make_entry()

    token = current_entry.set(entry)
    try:
        with _base_patches(static_load_side_effect=StaticGtfsLoadError("HTTP 500")):
            result = await async_setup_entry(hass, entry)

            with caplog.at_level(logging.WARNING, logger="custom_components.tfi_live"):
                await entry.background_coros[0]
    finally:
        current_entry.reset(token)

    assert result is True
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) >= 1


# ---------------------------------------------------------------------------
# TC-4: GtfsRtAuthError propagates ConfigEntryAuthFailed
# ---------------------------------------------------------------------------


async def test_setup_entry_gtfs_rt_auth_error_raises_config_entry_auth_failed() -> None:
    """TC-4: GtfsRtAuthError during first refresh propagates auth failure.

    Arrange: StaticGtfsClient.async_load succeeds; GtfsRtClient raises
        GtfsRtAuthError.
    Act: call async_setup_entry.
    Assert:
        - raises ConfigEntryAuthFailed
        - entry.async_start_reauth was called exactly once
    """
    hass = _make_hass()
    entry = _make_entry()

    token = current_entry.set(entry)
    try:
        with _base_patches(rt_side_effect=GtfsRtAuthError("HTTP 401")):
            with pytest.raises(ConfigEntryAuthFailed):
                await async_setup_entry(hass, entry)
    finally:
        current_entry.reset(token)
        for coro in entry.background_coros:
            coro.close()

    entry.async_start_reauth.assert_called_once()


# ---------------------------------------------------------------------------
# Issue #99: config entry migration strips format=json from the trip URL
# ---------------------------------------------------------------------------


def _make_migration_entry(
    *,
    trip_update_url: str,
    version: int = 1,
    minor_version: int = 1,
    data_sensors: list[dict] | None = None,
    options: dict | None = None,
) -> MagicMock:
    """Return a MagicMock ConfigEntry for async_migrate_entry tests.

    Args:
        trip_update_url: The stored trip update feed URL.
        version: The entry's major schema version.
        minor_version: The entry's minor schema version.
        data_sensors: Sensors to seed under ``entry.data[CONF_SENSORS]``,
            mimicking a pre-#144 entry that has never been through the
            options flow. Defaults to an empty list when omitted.
        options: The entry's ``options`` dict. Defaults to empty, mimicking
            an entry that has never been through the options flow.

    Returns:
        MagicMock with ``version``, ``minor_version``, a ``data`` dict
        containing the trip update URL, and an ``options`` dict.
    """
    entry = MagicMock()
    entry.version = version
    entry.minor_version = minor_version
    entry.data = {
        CONF_API_KEY: _DUMMY_API_KEY,
        CONF_TRIP_UPDATE_URL: trip_update_url,
        CONF_STATIC_GTFS_URL: _DUMMY_STATIC_GTFS_URL,
        CONF_SENSORS: data_sensors if data_sensors is not None else [],
    }
    entry.options = options if options is not None else {}
    return entry


async def test_migrate_entry_strips_format_json_from_old_default() -> None:
    """Issue #99: a v1.1 entry with the old JSON default is rewritten to protobuf."""
    hass = _make_hass()
    entry = _make_migration_entry(
        trip_update_url=(
            "https://api.nationaltransport.ie/gtfsr/v2/TripUpdates?format=json"
        )
    )

    result = await async_migrate_entry(hass, entry)

    assert result is True
    # Two migration steps run for a fresh v1.1 entry: the URL fix (1.2) and
    # the sensors-to-options move (1.3, #144). The URL fix is the first call.
    first_call_kwargs = hass.config_entries.async_update_entry.call_args_list[0][1]
    assert first_call_kwargs["minor_version"] == 2
    assert (
        first_call_kwargs["data"][CONF_TRIP_UPDATE_URL]
        == "https://api.nationaltransport.ie/gtfsr/v2/TripUpdates"
    )


async def test_migrate_entry_preserves_other_query_params() -> None:
    """Issue #99: migration removes only format=json, keeping other parameters."""
    hass = _make_hass()
    entry = _make_migration_entry(
        trip_update_url="https://example.com/feed?a=1&format=json&b=2"
    )

    result = await async_migrate_entry(hass, entry)

    assert result is True
    first_call_kwargs = hass.config_entries.async_update_entry.call_args_list[0][1]
    assert first_call_kwargs["data"][CONF_TRIP_UPDATE_URL] == (
        "https://example.com/feed?a=1&b=2"
    )


async def test_migrate_entry_clean_url_bumps_minor_version_only() -> None:
    """Issue #99: a v1.1 entry with a clean URL is bumped to 1.2 unchanged."""
    hass = _make_hass()
    entry = _make_migration_entry(trip_update_url=_DUMMY_TRIP_UPDATE_URL)

    result = await async_migrate_entry(hass, entry)

    assert result is True
    first_call_kwargs = hass.config_entries.async_update_entry.call_args_list[0][1]
    assert first_call_kwargs["minor_version"] == 2
    assert first_call_kwargs["data"][CONF_TRIP_UPDATE_URL] == _DUMMY_TRIP_UPDATE_URL


async def test_migrate_entry_current_version_is_noop() -> None:
    """A v1.3 entry needs no migration and the entry data is untouched."""
    hass = _make_hass()
    entry = _make_migration_entry(
        trip_update_url=_DUMMY_TRIP_UPDATE_URL,
        minor_version=3,
        options={CONF_SENSORS: []},
    )

    result = await async_migrate_entry(hass, entry)

    assert result is True
    hass.config_entries.async_update_entry.assert_not_called()


async def test_migrate_entry_future_version_returns_false() -> None:
    """An entry created by a newer major version cannot be migrated."""
    hass = _make_hass()
    entry = _make_migration_entry(trip_update_url=_DUMMY_TRIP_UPDATE_URL, version=2)

    result = await async_migrate_entry(hass, entry)

    assert result is False
    hass.config_entries.async_update_entry.assert_not_called()


# ---------------------------------------------------------------------------
# Issue #144: config entry migration moves sensors from data to options
# ---------------------------------------------------------------------------


async def test_migrate_entry_moves_sensors_from_data_to_options() -> None:
    """A pre-#144 entry (never through the options flow) has sensors moved.

    Such an entry has its sensors only under entry.data[CONF_SENSORS] and an
    empty entry.options. Migration must copy them to entry.options and strip
    them from entry.data, or every sensor for this entry would silently
    vanish once the read sites switch to entry.options.
    """
    hass = _make_hass()
    sensors = [{"name": "Existing", "stop_id": "S1", "route_id": "R1"}]
    entry = _make_migration_entry(
        trip_update_url=_DUMMY_TRIP_UPDATE_URL,
        minor_version=2,
        data_sensors=sensors,
        options={},
    )

    result = await async_migrate_entry(hass, entry)

    assert result is True
    call_kwargs = hass.config_entries.async_update_entry.call_args[1]
    assert call_kwargs["minor_version"] == 3
    assert call_kwargs["options"][CONF_SENSORS] == sensors
    assert CONF_SENSORS not in call_kwargs["data"]


async def test_migrate_entry_keeps_existing_options_sensors() -> None:
    """A post-#144-options-flow entry already has sensors under options.

    Such an entry's stale entry.data[CONF_SENSORS] copy (from the old
    options flow's ``async_create_entry`` call, which HA stores as
    entry.options for an OptionsFlow — the value under entry.data itself is
    untouched) must not overwrite the real, possibly newer, options copy.
    """
    hass = _make_hass()
    stale_data_sensors = [{"name": "Stale", "stop_id": "S0", "route_id": "R0"}]
    real_sensors = [
        {"name": "Existing", "stop_id": "S1", "route_id": "R1"},
        {"name": "Added via options flow", "stop_id": "S2", "route_id": "R2"},
    ]
    entry = _make_migration_entry(
        trip_update_url=_DUMMY_TRIP_UPDATE_URL,
        minor_version=2,
        data_sensors=stale_data_sensors,
        options={CONF_SENSORS: real_sensors},
    )

    result = await async_migrate_entry(hass, entry)

    assert result is True
    call_kwargs = hass.config_entries.async_update_entry.call_args[1]
    assert call_kwargs["minor_version"] == 3
    assert call_kwargs["options"][CONF_SENSORS] == real_sensors
    assert CONF_SENSORS not in call_kwargs["data"]


async def test_migrate_entry_sensors_already_current_is_noop() -> None:
    """A v1.3 entry with sensors already in options needs no migration."""
    hass = _make_hass()
    entry = _make_migration_entry(
        trip_update_url=_DUMMY_TRIP_UPDATE_URL,
        minor_version=3,
        options={CONF_SENSORS: [{"name": "X", "stop_id": "S1", "route_id": "R1"}]},
    )

    result = await async_migrate_entry(hass, entry)

    assert result is True
    hass.config_entries.async_update_entry.assert_not_called()


# ---------------------------------------------------------------------------
# TC-5: Unload returns True
# ---------------------------------------------------------------------------


async def test_unload_entry_returns_true() -> None:
    """TC-5: async_unload_entry returns True after a successful setup.

    Arrange: perform a successful setup (same conditions as TC-1), then call
        async_unload_entry.
    Assert:
        - async_unload_entry returns True
    """
    hass = _make_hass()
    entry = _make_entry()

    token = current_entry.set(entry)
    try:
        with _base_patches():
            await async_setup_entry(hass, entry)
    finally:
        current_entry.reset(token)
        for coro in entry.background_coros:
            coro.close()

    assert entry.runtime_data is not None

    result = await async_unload_entry(hass, entry)
    assert result is True


# ---------------------------------------------------------------------------
# Issue #111: stop-wide sensor coexists with a route-filtered sensor
# ---------------------------------------------------------------------------


async def test_stop_wide_and_route_filtered_sensors_coexist_on_same_stop() -> None:
    """Issue #111: route_id=None and route_id="46A" sensors on the same stop.

    Arrange: entry with two sensors configured for the same stop — one
        stop-wide (``route_id`` unset) and one route-filtered (``route_id`` =
        "46A"); RT feed containing trip updates for two distinct routes
        ("46A" and "145") both serving that stop.
    Act: run the full integration setup (``async_setup_entry``), then forward
        to the sensor platform's ``async_setup_entry`` using the resulting
        coordinator, exactly as Home Assistant would via
        ``async_forward_entry_setups``.
    Assert:
        - both entities are created without error
        - the two sensors get distinct unique_ids
        - the route-filtered sensor only sees the "46A" departure
        - the stop-wide sensor merges departures across both routes
    """
    hass = _make_hass()
    entry = _make_entry(
        sensors=[
            {"stop_id": "STOP_A", "route_id": None, "name": "All Routes"},
            {"stop_id": "STOP_A", "route_id": "46A", "name": "46A Only"},
        ]
    )

    # Departure timestamps are computed relative to "now" (rather than reusing
    # the fixed timestamps in _VALID_TRIP_UPDATE) so they fall inside the
    # sensor's grace window regardless of when the test suite runs.
    now_ts = int(datetime.now(UTC).timestamp())
    route_46a_update = TripUpdate(
        trip_id="TRIP_001",
        route_id="46A",
        direction_id="0",
        start_date="20260517",
        stop_time_updates=[
            StopTimeUpdate(
                stop_id="STOP_A",
                arrival_delay=120,
                departure_delay=120,
                arrival_time=now_ts + 300,
                departure_time=now_ts + 360,
            )
        ],
    )
    route_145_update = TripUpdate(
        trip_id="TRIP_002",
        route_id="145",
        direction_id="0",
        start_date="20260517",
        stop_time_updates=[
            StopTimeUpdate(
                stop_id="STOP_A",
                arrival_delay=0,
                departure_delay=0,
                arrival_time=now_ts + 400,
                departure_time=now_ts + 460,
            )
        ],
    )

    token = current_entry.set(entry)
    try:
        with _base_patches(rt_return_value=[route_46a_update, route_145_update]):
            result = await async_setup_entry(hass, entry)
    finally:
        current_entry.reset(token)
        for coro in entry.background_coros:
            coro.close()

    assert result is True

    added: list[TfiLiveSensor] = []

    def _add_entities(entities, update_before_add=False):
        added.extend(entities)

    await sensor_async_setup_entry(hass, entry, _add_entities)

    assert len(added) == 2
    stop_wide, filtered = added
    assert stop_wide.unique_id != filtered.unique_id

    filtered_trip_ids = {d["trip_id"] for d in filtered._get_departures()}
    stop_wide_trip_ids = {d["trip_id"] for d in stop_wide._get_departures()}

    assert filtered_trip_ids == {"TRIP_001"}
    assert stop_wide_trip_ids == {"TRIP_001", "TRIP_002"}
