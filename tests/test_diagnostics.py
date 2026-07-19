"""Tests for custom_components.tfi_live.diagnostics.

Covers async_get_config_entry_diagnostics: verifies that the API key is
redacted and that the expected top-level keys are present.
"""

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from custom_components.tfi_live.diagnostics import async_get_config_entry_diagnostics


@pytest.fixture
def mock_hass() -> MagicMock:
    """Return a minimal mocked HomeAssistant instance.

    Returns:
        MagicMock standing in for a HomeAssistant object.
    """
    return MagicMock()


@pytest.fixture
def mock_entry() -> MagicMock:
    """Return a mock config entry with a coordinator on runtime_data.

    The entry data includes api_key and feed URL fields; sensors live under
    entry.options, matching entry.options[CONF_SENSORS] as the single
    source of truth.
    The coordinator exposes ``_last_successful_fetch``, ``last_update_success``,
    and ``data`` matching the real TfiLiveCoordinator shape.

    Returns:
        MagicMock standing in for a ConfigEntry.
    """
    coordinator = MagicMock()
    coordinator._last_successful_fetch = datetime(2026, 5, 18, 12, 0, 0)
    coordinator.last_update_success = True
    coordinator.data = {"entities": [{"trip_id": "T1"}, {"trip_id": "T2"}]}

    entry = MagicMock()
    entry.runtime_data = coordinator
    entry.data = {
        "api_key": "super-secret-key",
        "trip_update_url": "https://example.com/trips",
        "static_gtfs_url": "https://example.com/gtfs.zip",
    }
    entry.options = {
        "sensors": [
            {
                "name": "Next 46A",
                "stop_id": "STOP_A",
                "route_id": "46A",
                "direction_id": None,
                "operator_id": None,
            }
        ],
    }
    return entry


async def test_diagnostics_api_key_is_redacted(
    mock_hass: MagicMock,
    mock_entry: MagicMock,
) -> None:
    """api_key in config_entry output is replaced with **REDACTED**."""
    result = await async_get_config_entry_diagnostics(mock_hass, mock_entry)

    assert result["config_entry"]["api_key"] == "**REDACTED**"


async def test_diagnostics_top_level_keys_present(
    mock_hass: MagicMock,
    mock_entry: MagicMock,
) -> None:
    """Result dict contains the three expected top-level keys."""
    result = await async_get_config_entry_diagnostics(mock_hass, mock_entry)

    assert "config_entry" in result
    assert "coordinator" in result
    assert "sensors" in result


async def test_diagnostics_coordinator_state(
    mock_hass: MagicMock,
    mock_entry: MagicMock,
) -> None:
    """coordinator block contains last_successful_fetch, last_update_success,
    and entity_count with correct values."""
    result = await async_get_config_entry_diagnostics(mock_hass, mock_entry)

    coord = result["coordinator"]
    assert coord["last_successful_fetch"] == "2026-05-18T12:00:00"
    assert coord["last_update_success"] is True
    assert coord["entity_count"] == 2


async def test_diagnostics_sensors_strip_api_key(
    mock_hass: MagicMock,
    mock_entry: MagicMock,
) -> None:
    """Sensor entries do not expose api_key even if present in sensor config."""
    # Inject an api_key into a sensor config to verify it is stripped.
    mock_entry.options = {
        **mock_entry.options,
        "sensors": [
            {
                "name": "Next 46A",
                "stop_id": "STOP_A",
                "route_id": "46A",
                "direction_id": None,
                "operator_id": None,
                "api_key": "should-be-removed",
            }
        ],
    }

    result = await async_get_config_entry_diagnostics(mock_hass, mock_entry)

    for sensor in result["sensors"]:
        assert "api_key" not in sensor


async def test_diagnostics_coordinator_none_last_fetch(
    mock_hass: MagicMock,
    mock_entry: MagicMock,
) -> None:
    """When _last_successful_fetch is None, coordinator shows None."""
    mock_entry.runtime_data._last_successful_fetch = None

    result = await async_get_config_entry_diagnostics(mock_hass, mock_entry)

    assert result["coordinator"]["last_successful_fetch"] is None


async def test_diagnostics_coordinator_none_data(
    mock_hass: MagicMock,
    mock_entry: MagicMock,
) -> None:
    """When coordinator.data is None, entity_count is None."""
    mock_entry.runtime_data.data = None

    result = await async_get_config_entry_diagnostics(mock_hass, mock_entry)

    assert result["coordinator"]["entity_count"] is None
