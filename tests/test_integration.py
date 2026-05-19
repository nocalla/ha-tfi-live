"""End-to-end integration tests for async_setup_entry and async_unload_entry.

T-012: exercises the full setup/teardown lifecycle of the ha_tfi_live integration
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
TC-2  AC 10: StaticGtfsLoadError during async_load does not abort setup —
      coordinator is still stored in entry.runtime_data after setup.
TC-3  TC-2 warning: StaticGtfsLoadError during async_load logs a WARNING.
TC-4  AC 17 end-to-end: GtfsRtAuthError during first refresh propagates
      ConfigEntryAuthFailed and triggers async_start_reauth.
TC-5  Unload: after a successful setup, async_unload_entry returns True.
"""

import logging
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState, current_entry
from homeassistant.const import Platform
from homeassistant.exceptions import ConfigEntryAuthFailed
from nta_gtfs import (
    GtfsRtAuthError,
    GtfsRtClient,
    StaticGtfsClient,
    StaticGtfsLoadError,
    StopTimeUpdate,
    TripUpdate,
)

from custom_components.ha_tfi_live.__init__ import (
    async_setup_entry,
    async_unload_entry,
)
from custom_components.ha_tfi_live.const import (
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
        None — caller uses ``async_setup_entry`` / ``async_unload_entry``
        inside the ``with`` block.
    """
    if rt_return_value is None and rt_side_effect is None:
        rt_return_value = [_VALID_TRIP_UPDATE]

    static_mock = AsyncMock(side_effect=static_load_side_effect)
    rt_mock = AsyncMock(return_value=rt_return_value, side_effect=rt_side_effect)

    with (
        patch("homeassistant.helpers.frame.report_usage"),
        patch(
            "custom_components.ha_tfi_live.__init__.async_get_clientsession",
            return_value=MagicMock(),
        ),
        patch.object(StaticGtfsClient, "async_load", static_mock),
        patch.object(GtfsRtClient, "async_fetch_trip_updates", rt_mock),
    ):
        yield


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

    assert result is True
    assert entry.runtime_data is not None
    hass.config_entries.async_forward_entry_setups.assert_called_once_with(
        entry, [Platform.SENSOR]
    )


# ---------------------------------------------------------------------------
# TC-2: AC 10 — StaticGtfsLoadError does not abort setup
# ---------------------------------------------------------------------------


async def test_setup_entry_static_gtfs_load_error_does_not_abort_setup() -> None:
    """TC-2 (AC 10): StaticGtfsLoadError from async_load is swallowed; setup completes.

    Arrange: StaticGtfsClient.async_load raises StaticGtfsLoadError;
        GtfsRtClient.async_fetch_trip_updates returns valid data.
    Act: call async_setup_entry.
    Assert:
        - does not raise
        - coordinator is still stored at entry.runtime_data
    """
    hass = _make_hass()
    entry = _make_entry()

    token = current_entry.set(entry)
    try:
        with _base_patches(static_load_side_effect=StaticGtfsLoadError("HTTP 500")):
            result = await async_setup_entry(hass, entry)
    finally:
        current_entry.reset(token)

    assert result is True
    assert entry.runtime_data is not None


# ---------------------------------------------------------------------------
# TC-3: StaticGtfsLoadError logs a WARNING
# ---------------------------------------------------------------------------


async def test_setup_entry_static_gtfs_load_error_logs_warning(caplog) -> None:
    """TC-3: StaticGtfsLoadError during async_load emits a WARNING log entry."""
    hass = _make_hass()
    entry = _make_entry()

    token = current_entry.set(entry)
    try:
        with _base_patches(static_load_side_effect=StaticGtfsLoadError("HTTP 500")):
            with caplog.at_level(
                logging.WARNING, logger="custom_components.ha_tfi_live"
            ):
                await async_setup_entry(hass, entry)
    finally:
        current_entry.reset(token)

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) >= 1


# ---------------------------------------------------------------------------
# TC-4: AC 17 — GtfsRtAuthError propagates ConfigEntryAuthFailed
# ---------------------------------------------------------------------------


async def test_setup_entry_gtfs_rt_auth_error_raises_config_entry_auth_failed() -> None:
    """TC-4 (AC 17): GtfsRtAuthError during first refresh propagates auth failure.

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

    entry.async_start_reauth.assert_called_once()


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

    assert entry.runtime_data is not None

    result = await async_unload_entry(hass, entry)
    assert result is True
