"""Tests for custom_components.tfi_live.coordinator.TfiLiveCoordinator.

Covers: update interval (AC 14), successful fetch parsing, last-successful-fetch
tracking, HTTP 500 error handling and log deduplication (AC 15, 16), HTTP 401
re-auth trigger and ERROR log (AC 17), bad JSON handling and ERROR log (AC 26),
network error handling, and direction_id string coercion.

All HTTP interactions are mocked via unittest.mock — no live network calls are
made.  HomeAssistant and ConfigEntry are replaced with MagicMock objects.
"""

import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.tfi_live.coordinator import TfiLiveCoordinator

# ---------------------------------------------------------------------------
# Minimal valid GTFS-RT JSON payload
# ---------------------------------------------------------------------------

VALID_PAYLOAD = {
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
def mock_cache():
    """Return a MagicMock standing in for a StaticGtfsCache."""
    return MagicMock()


@pytest.fixture
def coordinator(mock_hass, mock_entry, mock_cache):
    """Construct a TfiLiveCoordinator with mocked dependencies.

    Patches ``homeassistant.helpers.frame.report_usage`` to a no-op during
    construction.  The production coordinator calls ``DataUpdateCoordinator``
    without passing ``config_entry`` explicitly, which triggers a ContextVar
    look-up and a ``frame.report_usage`` call that requires a live HA event
    loop context.  The patch prevents the crash in unit-test environments.
    """
    with patch("homeassistant.helpers.frame.report_usage"):
        coord = TfiLiveCoordinator(mock_hass, mock_entry, mock_cache)
    return coord


# ---------------------------------------------------------------------------
# Helper: build a mock aiohttp ClientSession whose GET returns a fixed status
# and body.
# ---------------------------------------------------------------------------


def _make_mock_session(status: int, body: bytes | None = None) -> MagicMock:
    """Return a mock aiohttp.ClientSession for a fixed HTTP response.

    Args:
        status: HTTP status code to return.
        body: Raw bytes for ``resp.text()``; defaults to ``b""`` when ``None``.

    Returns:
        MagicMock whose ``get`` method operates as an async context manager
        that yields a response object with the given ``status`` and ``text``
        coroutine.
    """
    body_str = (body or b"").decode()

    resp = MagicMock()
    resp.status = status
    resp.text = AsyncMock(return_value=body_str)

    # async context manager: __aenter__ returns resp, __aexit__ is a no-op
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)

    session = MagicMock()
    session.get = MagicMock(return_value=cm)
    return session


def _make_raising_session(exc: Exception) -> MagicMock:
    """Return a mock aiohttp.ClientSession whose GET raises ``exc``.

    Args:
        exc: Exception to raise when entering the GET context manager.

    Returns:
        MagicMock whose ``get`` context manager raises on ``__aenter__``.
    """
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(side_effect=exc)
    cm.__aexit__ = AsyncMock(return_value=False)

    session = MagicMock()
    session.get = MagicMock(return_value=cm)
    return session


# ---------------------------------------------------------------------------
# TC-1: update_interval
# ---------------------------------------------------------------------------


def test_update_interval_is_60_seconds(coordinator):
    """TC-1: coordinator.update_interval equals timedelta(seconds=60)."""
    # Arrange — coordinator is already constructed by the fixture
    # Act / Assert
    assert coordinator.update_interval == timedelta(seconds=60)
    assert coordinator.update_interval.total_seconds() == 60


# ---------------------------------------------------------------------------
# TC-2: successful fetch returns expected structure
# ---------------------------------------------------------------------------


async def test_successful_fetch_returns_entities(coordinator):
    """TC-2: A 200 response with valid JSON is parsed into the expected dict.

    The returned dict must contain key ``"entities"`` whose value is a list
    with one entry having ``trip_id == "TRIP_001"``, ``route_id == "46A"``,
    and ``direction_id == "0"`` (string, not integer).
    """
    # Arrange
    mock_session = _make_mock_session(200, body=json.dumps(VALID_PAYLOAD).encode())

    # Act
    with patch(
        "custom_components.tfi_live.coordinator.async_get_clientsession",
        return_value=mock_session,
    ):
        result = await coordinator._async_update_data()

    # Assert structure
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
    # Arrange
    mock_session = _make_mock_session(200, body=json.dumps(VALID_PAYLOAD).encode())

    # Act
    with patch(
        "custom_components.tfi_live.coordinator.async_get_clientsession",
        return_value=mock_session,
    ):
        await coordinator._async_update_data()

    # Assert
    assert coordinator.last_successful_fetch is not None
    assert isinstance(coordinator.last_successful_fetch, datetime)


# ---------------------------------------------------------------------------
# TC-4: _last_successful_fetch is None before any call
# ---------------------------------------------------------------------------


def test_last_successful_fetch_is_none_before_any_call(coordinator):
    """TC-4: _last_successful_fetch is None immediately after construction."""
    assert coordinator.last_successful_fetch is None


# ---------------------------------------------------------------------------
# TC-5: HTTP 500 raises UpdateFailed
# ---------------------------------------------------------------------------


async def test_http_500_raises_update_failed(coordinator):
    """TC-5: A 500 response causes _async_update_data to raise UpdateFailed."""
    # Arrange
    mock_session = _make_mock_session(500)

    # Act / Assert
    with patch(
        "custom_components.tfi_live.coordinator.async_get_clientsession",
        return_value=mock_session,
    ):
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()


# ---------------------------------------------------------------------------
# TC-6: HTTP 500 logs exactly one WARNING (deduplication)
# ---------------------------------------------------------------------------


async def test_http_500_logs_warning_once(coordinator, caplog):
    """TC-6: Two consecutive 500 responses produce exactly one WARNING log."""
    # Arrange
    mock_session = _make_mock_session(500)

    import logging

    # Act — call twice, catching UpdateFailed each time
    with patch(
        "custom_components.tfi_live.coordinator.async_get_clientsession",
        return_value=mock_session,
    ):
        with caplog.at_level(
            logging.WARNING, logger="custom_components.tfi_live.coordinator"
        ):
            for _ in range(2):
                with pytest.raises(UpdateFailed):
                    await coordinator._async_update_data()

    # Assert — exactly one WARNING entry
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1


# ---------------------------------------------------------------------------
# TC-7: second HTTP 500 call does NOT produce a second log entry
# ---------------------------------------------------------------------------


async def test_http_500_second_call_no_extra_log(coordinator, caplog):
    """TC-7: The second consecutive 500 emits zero additional log entries.

    This test explicitly checks the deduplication key ``"http_500"`` is
    stable across back-to-back failures.
    """
    import logging

    mock_session = _make_mock_session(500)

    with patch(
        "custom_components.tfi_live.coordinator.async_get_clientsession",
        return_value=mock_session,
    ):
        with caplog.at_level(
            logging.WARNING, logger="custom_components.tfi_live.coordinator"
        ):
            # First call
            with pytest.raises(UpdateFailed):
                await coordinator._async_update_data()

            first_call_count = len(caplog.records)

            # Second call
            with pytest.raises(UpdateFailed):
                await coordinator._async_update_data()

            second_call_count = len(caplog.records)

    # No additional log records were emitted on the second call
    assert second_call_count == first_call_count


# ---------------------------------------------------------------------------
# TC-8: HTTP 401 raises ConfigEntryAuthFailed and calls async_start_reauth
# ---------------------------------------------------------------------------


async def test_http_401_raises_config_entry_auth_failed(coordinator, mock_entry):
    """TC-8: A 401 response raises ConfigEntryAuthFailed and triggers reauth."""
    # Arrange
    mock_session = _make_mock_session(401)

    # Act / Assert
    with patch(
        "custom_components.tfi_live.coordinator.async_get_clientsession",
        return_value=mock_session,
    ):
        with pytest.raises(ConfigEntryAuthFailed):
            await coordinator._async_update_data()

    mock_entry.async_start_reauth.assert_called_once()


# ---------------------------------------------------------------------------
# TC-9: HTTP 401 logs exactly one ERROR
# ---------------------------------------------------------------------------


async def test_http_401_logs_error_once(coordinator, caplog):
    """TC-9: A 401 response emits exactly one ERROR log entry."""
    import logging

    mock_session = _make_mock_session(401)

    with patch(
        "custom_components.tfi_live.coordinator.async_get_clientsession",
        return_value=mock_session,
    ):
        with caplog.at_level(
            logging.ERROR, logger="custom_components.tfi_live.coordinator"
        ):
            with pytest.raises(ConfigEntryAuthFailed):
                await coordinator._async_update_data()

    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1


# ---------------------------------------------------------------------------
# TC-10: Bad JSON raises UpdateFailed
# ---------------------------------------------------------------------------


async def test_bad_json_raises_update_failed(coordinator):
    """TC-10: A 200 response with non-JSON body raises UpdateFailed."""
    # Arrange
    mock_session = _make_mock_session(200, body=b"not json")

    # Act / Assert
    with patch(
        "custom_components.tfi_live.coordinator.async_get_clientsession",
        return_value=mock_session,
    ):
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()


# ---------------------------------------------------------------------------
# TC-11: Bad JSON logs an ERROR
# ---------------------------------------------------------------------------


async def test_bad_json_logs_error(coordinator, caplog):
    """TC-11: A 200 response with non-JSON body emits an ERROR log."""
    import logging

    mock_session = _make_mock_session(200, body=b"not json")

    with patch(
        "custom_components.tfi_live.coordinator.async_get_clientsession",
        return_value=mock_session,
    ):
        with caplog.at_level(
            logging.ERROR, logger="custom_components.tfi_live.coordinator"
        ):
            with pytest.raises(UpdateFailed):
                await coordinator._async_update_data()

    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) >= 1


# ---------------------------------------------------------------------------
# TC-12: Network error (ClientError) raises UpdateFailed
# ---------------------------------------------------------------------------


async def test_network_error_raises_update_failed(coordinator):
    """TC-12: An aiohttp.ClientError during GET raises UpdateFailed."""
    # Arrange
    mock_session = _make_raising_session(
        aiohttp.ClientConnectionError("connection refused")
    )

    # Act / Assert
    with patch(
        "custom_components.tfi_live.coordinator.async_get_clientsession",
        return_value=mock_session,
    ):
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()


# ---------------------------------------------------------------------------
# TC-13: Network error logs a WARNING
# ---------------------------------------------------------------------------


async def test_network_error_logs_warning(coordinator, caplog):
    """TC-13: An aiohttp.ClientError emits a WARNING log."""
    import logging

    mock_session = _make_raising_session(
        aiohttp.ClientConnectionError("connection refused")
    )

    with patch(
        "custom_components.tfi_live.coordinator.async_get_clientsession",
        return_value=mock_session,
    ):
        with caplog.at_level(
            logging.WARNING, logger="custom_components.tfi_live.coordinator"
        ):
            with pytest.raises(UpdateFailed):
                await coordinator._async_update_data()

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) >= 1


# ---------------------------------------------------------------------------
# TC-14: _parse_feed coerces direction_id to string
# ---------------------------------------------------------------------------


def test_parse_feed_direction_id_as_string():
    """TC-14: _parse_feed converts integer direction_id to the string ``"0"``.

    The GTFS-RT payload carries ``direction_id`` as an integer; the
    coordinator must normalise it to a string before returning.
    """
    # Act
    entities = TfiLiveCoordinator._parse_feed(VALID_PAYLOAD)

    # Assert
    assert len(entities) == 1
    direction_id = entities[0]["direction_id"]
    assert direction_id == "0"
    assert isinstance(direction_id, str)
    assert not isinstance(direction_id, int)
