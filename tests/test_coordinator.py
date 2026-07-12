"""Tests for custom_components.ha_tfi_live.coordinator.TfiLiveCoordinator.

Covers: update interval, successful fetch parsing, last-successful-fetch
tracking, GtfsRtFetchError handling and log deduplication,
GtfsRtAuthError re-auth trigger and ERROR log, GtfsRtParseError handling
and ERROR log, and direction_id string coercion.

All interactions with GtfsRtClient are mocked — no live network calls are made.
HomeAssistant and ConfigEntry are replaced with MagicMock objects.
"""

import logging
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from nta_gtfs import (
    GtfsRtAuthError,
    GtfsRtFetchError,
    GtfsRtParseError,
    StopTimeUpdate,
    TripUpdate,
)

from custom_components.ha_tfi_live.coordinator import TfiLiveCoordinator

# ---------------------------------------------------------------------------
# A minimal TripUpdate returned by the mocked GtfsRtClient
# ---------------------------------------------------------------------------

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
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_hass():
    """Return a minimal MagicMock standing in for HomeAssistant."""
    hass = MagicMock()
    hass.data = {}
    return hass


@pytest.fixture
def mock_entry():
    """Return a MagicMock standing in for a ConfigEntry.

    Provides ``data`` dict with ``trip_update_url`` and ``api_key`` keys, plus
    an ``entry_id`` attribute and a callable ``async_start_reauth`` stub.
    """
    entry = MagicMock()
    entry.data = {
        "trip_update_url": "https://example.com/gtfs-rt",
        "api_key": "test-key",
    }
    entry.entry_id = "test_entry_id"
    return entry


@pytest.fixture
def mock_rt_client():
    """Return a MagicMock GtfsRtClient with async_fetch_trip_updates as AsyncMock.

    Defaults to returning a single valid TripUpdate on each call.
    """
    client = MagicMock()
    client.async_fetch_trip_updates = AsyncMock(return_value=[_VALID_TRIP_UPDATE])
    return client


@pytest.fixture
def mock_cache():
    """Return a MagicMock standing in for a StaticGtfsClient."""
    return MagicMock()


@pytest.fixture
def coordinator(mock_hass, mock_entry, mock_rt_client, mock_cache):
    """Construct a TfiLiveCoordinator with mocked dependencies.

    Patches ``homeassistant.helpers.frame.report_usage`` to a no-op during
    construction.  The production coordinator calls ``DataUpdateCoordinator``
    without passing ``config_entry`` explicitly, which triggers a ContextVar
    look-up and a ``frame.report_usage`` call that requires a live HA event
    loop context.  The patch prevents the crash in unit-test environments.
    """
    with patch("homeassistant.helpers.frame.report_usage"):
        coord = TfiLiveCoordinator(mock_hass, mock_entry, mock_rt_client, mock_cache)
    return coord


# ---------------------------------------------------------------------------
# TC-1: update_interval
# ---------------------------------------------------------------------------


def test_update_interval_is_60_seconds(coordinator):
    """TC-1: coordinator.update_interval equals timedelta(seconds=60)."""
    assert coordinator.update_interval == timedelta(seconds=60)
    assert coordinator.update_interval.total_seconds() == 60


# ---------------------------------------------------------------------------
# TC-2: successful fetch returns expected structure
# ---------------------------------------------------------------------------


async def test_successful_fetch_returns_entities(coordinator):
    """TC-2: async_fetch_trip_updates() result is converted to the expected dict.

    The returned dict must contain key ``"entities"`` whose value is a list
    with one entry having ``trip_id == "TRIP_001"``, ``route_id == "46A"``,
    and ``direction_id == "0"`` (string, not integer).
    """
    result = await coordinator._async_update_data()

    assert isinstance(result, dict)
    assert "entities" in result

    entities = result["entities"]
    assert len(entities) == 1

    entity = entities[0]
    assert entity["trip_id"] == "TRIP_001"
    assert entity["route_id"] == "46A"
    assert entity["direction_id"] == "0"


# ---------------------------------------------------------------------------
# TC-3: _last_successful_fetch set after success
# ---------------------------------------------------------------------------


async def test_last_successful_fetch_set_on_success(coordinator):
    """TC-3: _last_successful_fetch is a datetime after a successful fetch."""
    await coordinator._async_update_data()

    assert coordinator.last_successful_fetch is not None
    assert isinstance(coordinator.last_successful_fetch, datetime)


# ---------------------------------------------------------------------------
# TC-4: _last_successful_fetch is None before any call
# ---------------------------------------------------------------------------


def test_last_successful_fetch_is_none_before_any_call(coordinator):
    """TC-4: _last_successful_fetch is None immediately after construction."""
    assert coordinator.last_successful_fetch is None


# ---------------------------------------------------------------------------
# TC-5: GtfsRtFetchError raises UpdateFailed
# ---------------------------------------------------------------------------


async def test_fetch_error_raises_update_failed(coordinator, mock_rt_client):
    """TC-5: GtfsRtFetchError from the client raises UpdateFailed."""
    mock_rt_client.async_fetch_trip_updates.side_effect = GtfsRtFetchError("HTTP 500")

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


# ---------------------------------------------------------------------------
# TC-6: GtfsRtFetchError logs exactly one WARNING (deduplication)
# ---------------------------------------------------------------------------


async def test_fetch_error_logs_warning_once(coordinator, mock_rt_client, caplog):
    """TC-6: Two consecutive GtfsRtFetchErrors produce exactly one WARNING log."""
    mock_rt_client.async_fetch_trip_updates.side_effect = GtfsRtFetchError("HTTP 500")

    with caplog.at_level(
        logging.WARNING, logger="custom_components.ha_tfi_live.coordinator"
    ):
        for _ in range(2):
            with pytest.raises(UpdateFailed):
                await coordinator._async_update_data()

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1


# ---------------------------------------------------------------------------
# TC-7: second GtfsRtFetchError does NOT produce a second log entry
# ---------------------------------------------------------------------------


async def test_fetch_error_second_call_no_extra_log(
    coordinator, mock_rt_client, caplog
):
    """TC-7: The second consecutive fetch error emits zero additional log entries."""
    mock_rt_client.async_fetch_trip_updates.side_effect = GtfsRtFetchError("HTTP 500")

    with caplog.at_level(
        logging.WARNING, logger="custom_components.ha_tfi_live.coordinator"
    ):
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()
        first_call_count = len(caplog.records)

        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()
        second_call_count = len(caplog.records)

    assert second_call_count == first_call_count


# ---------------------------------------------------------------------------
# TC-8: GtfsRtAuthError raises ConfigEntryAuthFailed and calls async_start_reauth
# ---------------------------------------------------------------------------


async def test_auth_error_raises_config_entry_auth_failed(
    coordinator, mock_rt_client, mock_entry
):
    """TC-8: GtfsRtAuthError raises ConfigEntryAuthFailed and triggers reauth."""
    mock_rt_client.async_fetch_trip_updates.side_effect = GtfsRtAuthError("HTTP 401")

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()

    mock_entry.async_start_reauth.assert_called_once()


# ---------------------------------------------------------------------------
# TC-9: GtfsRtAuthError logs exactly one ERROR
# ---------------------------------------------------------------------------


async def test_auth_error_logs_error_once(coordinator, mock_rt_client, caplog):
    """TC-9: GtfsRtAuthError emits exactly one ERROR log entry."""
    mock_rt_client.async_fetch_trip_updates.side_effect = GtfsRtAuthError("HTTP 401")

    with caplog.at_level(
        logging.ERROR, logger="custom_components.ha_tfi_live.coordinator"
    ):
        with pytest.raises(ConfigEntryAuthFailed):
            await coordinator._async_update_data()

    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1


# ---------------------------------------------------------------------------
# TC-10: GtfsRtParseError raises UpdateFailed
# ---------------------------------------------------------------------------


async def test_parse_error_raises_update_failed(coordinator, mock_rt_client):
    """TC-10: GtfsRtParseError from the client raises UpdateFailed."""
    mock_rt_client.async_fetch_trip_updates.side_effect = GtfsRtParseError("bad json")

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


# ---------------------------------------------------------------------------
# TC-11: GtfsRtParseError logs an ERROR
# ---------------------------------------------------------------------------


async def test_parse_error_logs_error(coordinator, mock_rt_client, caplog):
    """TC-11: GtfsRtParseError emits an ERROR log."""
    mock_rt_client.async_fetch_trip_updates.side_effect = GtfsRtParseError("bad json")

    with caplog.at_level(
        logging.ERROR, logger="custom_components.ha_tfi_live.coordinator"
    ):
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()

    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) >= 1


# ---------------------------------------------------------------------------
# TC-12: direction_id is passed through as a string in the output dict
# ---------------------------------------------------------------------------


async def test_direction_id_preserved_as_string(coordinator):
    """TC-12: direction_id from TripUpdate is passed through as a string.

    The library already normalises direction_id to str; the coordinator must
    not coerce it further.
    """
    result = await coordinator._async_update_data()

    direction_id = result["entities"][0]["direction_id"]
    assert direction_id == "0"
    assert isinstance(direction_id, str)
