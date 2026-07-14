"""Tests for custom_components.tfi_live.coordinator.TfiLiveCoordinator.

Covers: update interval, successful fetch parsing, last-successful-fetch
tracking, GtfsRtFetchError handling and log deduplication,
GtfsRtAuthError re-auth trigger and ERROR log, GtfsRtParseError handling
and ERROR log, and direction_id string coercion.

All interactions with GtfsRtClient are mocked — no live network calls are made.
HomeAssistant and ConfigEntry are replaced with MagicMock objects.
"""

import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import CoreState
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from nta_gtfs import (
    GtfsRtAuthError,
    GtfsRtFetchError,
    GtfsRtParseError,
    StaticGtfsLoadError,
    StopTimeUpdate,
    TripUpdate,
)

from custom_components.tfi_live.coordinator import TfiLiveCoordinator

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
    """Return a minimal MagicMock standing in for HomeAssistant.

    ``state`` defaults to ``CoreState.running`` so scheduling tests exercise
    the normal post-startup path; startup-deferral tests override it.
    """
    hass = MagicMock()
    hass.data = {}
    hass.state = CoreState.running
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
    # Close scheduled background coroutines so they never leak as
    # "coroutine was never awaited" warnings; tests that need to run the
    # coroutine override this side effect to capture it instead.
    entry.async_create_background_task = MagicMock(
        side_effect=lambda hass, coro, name=None: (coro.close(), MagicMock())[1]
    )
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
    """Return a MagicMock standing in for a StaticGtfsClient.

    ``loaded_at`` defaults to now (fresh data) so tests exercising the
    real-time fetch path do not trigger a background static refresh.
    """
    cache = MagicMock()
    cache.loaded_at = datetime.now(UTC)
    cache.async_refresh_if_stale = AsyncMock()
    return cache


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
        logging.WARNING, logger="custom_components.tfi_live.coordinator"
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
        logging.WARNING, logger="custom_components.tfi_live.coordinator"
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
        logging.ERROR, logger="custom_components.tfi_live.coordinator"
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
        logging.ERROR, logger="custom_components.tfi_live.coordinator"
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


# ---------------------------------------------------------------------------
# Background static GTFS refresh scheduling
# ---------------------------------------------------------------------------


async def test_static_refresh_scheduled_when_never_loaded(
    coordinator, mock_entry, mock_cache
):
    """A coordinator update schedules a background static load when unloaded."""
    mock_cache.loaded_at = None

    await coordinator._async_update_data()

    assert mock_entry.async_create_background_task.call_count == 1


async def test_static_refresh_scheduled_when_stale(coordinator, mock_entry, mock_cache):
    """A coordinator update schedules a background refresh of stale data."""
    mock_cache.loaded_at = datetime.now(UTC) - timedelta(hours=25)

    await coordinator._async_update_data()

    assert mock_entry.async_create_background_task.call_count == 1


async def test_static_refresh_skipped_when_fresh(coordinator, mock_entry, mock_cache):
    """No background refresh is scheduled while the static data is fresh."""
    await coordinator._async_update_data()

    mock_entry.async_create_background_task.assert_not_called()


async def test_static_refresh_not_rescheduled_while_in_flight(
    coordinator, mock_entry, mock_cache
):
    """A second update does not schedule a refresh while one is running."""
    mock_cache.loaded_at = None
    in_flight_task = MagicMock()
    in_flight_task.done.return_value = False
    mock_entry.async_create_background_task = MagicMock(
        side_effect=lambda hass, coro, name=None: (coro.close(), in_flight_task)[1]
    )

    await coordinator._async_update_data()
    await coordinator._async_update_data()

    assert mock_entry.async_create_background_task.call_count == 1


async def test_static_refresh_rescheduled_after_completion(
    coordinator, mock_entry, mock_cache
):
    """A new refresh is scheduled when the previous task finished but the
    data is still stale (i.e. the previous load failed)."""
    mock_cache.loaded_at = None
    done_task = MagicMock()
    done_task.done.return_value = True
    mock_entry.async_create_background_task = MagicMock(
        side_effect=lambda hass, coro, name=None: (coro.close(), done_task)[1]
    )

    await coordinator._async_update_data()
    await coordinator._async_update_data()

    assert mock_entry.async_create_background_task.call_count == 2


async def test_static_refresh_not_scheduled_when_fetch_fails(
    coordinator, mock_rt_client, mock_entry, mock_cache
):
    """No static load is scheduled when the RT fetch fails (#100).

    A failing update keeps the entry in setup-retry; scheduling the ~80 MB
    static load on every retry OOM-killed HA core on low-memory hosts.
    """
    mock_cache.loaded_at = None
    mock_rt_client.async_fetch_trip_updates.side_effect = GtfsRtFetchError("HTTP 500")

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()

    mock_entry.async_create_background_task.assert_not_called()


async def test_static_refresh_scheduled_on_failure_after_prior_success(
    coordinator, mock_rt_client, mock_entry, mock_cache
):
    """A failing update still schedules the refresh once setup has succeeded.

    Only setup-retry loops are blocked (#100); after the first successful
    refresh, static data must stay fresh even through an RT feed outage
    because sensors fall back to schedule data.
    """
    mock_cache.loaded_at = None

    await coordinator._async_update_data()
    assert mock_entry.async_create_background_task.call_count == 1

    mock_rt_client.async_fetch_trip_updates.side_effect = GtfsRtFetchError("HTTP 500")
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()

    assert mock_entry.async_create_background_task.call_count == 2


async def test_static_refresh_deferred_until_ha_started(
    coordinator, mock_hass, mock_entry, mock_cache
):
    """No static load is scheduled while HA is still starting (#100).

    The load is skipped during startup, when memory pressure is highest,
    and picked up by the first coordinator update after HA reaches
    ``CoreState.running``.
    """
    mock_cache.loaded_at = None
    mock_hass.state = CoreState.starting

    await coordinator._async_update_data()
    mock_entry.async_create_background_task.assert_not_called()

    mock_hass.state = CoreState.running
    await coordinator._async_update_data()
    assert mock_entry.async_create_background_task.call_count == 1


async def test_refresh_static_success_calls_refresh_if_stale(coordinator, mock_cache):
    """_async_refresh_static delegates to the cache's stale-aware refresh."""
    await coordinator._async_refresh_static()

    mock_cache.async_refresh_if_stale.assert_awaited_once()


async def test_refresh_static_failure_logs_warning_and_swallows(
    coordinator, mock_cache, caplog
):
    """A StaticGtfsLoadError is logged as a WARNING and not propagated."""
    mock_cache.async_refresh_if_stale = AsyncMock(
        side_effect=StaticGtfsLoadError("HTTP 500")
    )

    with caplog.at_level(logging.WARNING, logger="custom_components.tfi_live"):
        await coordinator._async_refresh_static()

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) >= 1


# ---------------------------------------------------------------------------
# Repairs issues for unmatched (stop_id, route_id) pairs (#102/#108)
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_ir():
    """Patch the issue_registry helpers imported into the coordinator module.

    ``async_get(hass).issues`` defaults to an empty dict, standing in for a
    registry with no pre-existing issues; tests covering stale-issue
    reconciliation override it.
    """
    with (
        patch("custom_components.tfi_live.coordinator.ir.async_create_issue") as create,
        patch("custom_components.tfi_live.coordinator.ir.async_delete_issue") as delete,
        patch("custom_components.tfi_live.coordinator.ir.async_get") as get_registry,
    ):
        get_registry.return_value.issues = {}
        yield create, delete, get_registry


async def test_unmatched_pair_creates_repairs_issue(
    coordinator, mock_entry, mock_cache, mock_ir
):
    """A pair absent from the static index raises a WARNING Repairs issue."""
    create, delete, _ = mock_ir
    mock_cache.available = True
    mock_cache.has_scheduled_pair = MagicMock(return_value=False)
    mock_entry.data["sensors"] = [
        {"name": "Farranlea Pk 220", "stop_id": "8370B2418801", "route_id": "220"}
    ]

    await coordinator._async_refresh_static()

    create.assert_called_once()
    delete.assert_not_called()
    _, kwargs = create.call_args
    assert kwargs["is_fixable"] is False
    assert kwargs["translation_key"] == "unmatched_stop_route_pair"
    assert kwargs["translation_placeholders"]["stop_id"] == "8370B2418801"
    assert kwargs["translation_placeholders"]["route_id"] == "220"
    assert "Farranlea Pk 220" in kwargs["translation_placeholders"]["sensor_names"]
    args, _ = create.call_args
    assert args[2] == f"{mock_entry.entry_id}_8370B2418801_220"


async def test_matched_pair_clears_repairs_issue(
    coordinator, mock_entry, mock_cache, mock_ir
):
    """A pair present in the static index clears any existing Repairs issue."""
    create, delete, _ = mock_ir
    mock_cache.available = True
    mock_cache.has_scheduled_pair = MagicMock(return_value=True)
    mock_entry.data["sensors"] = [
        {"name": "Farranlea Pk 220", "stop_id": "8370B2418801", "route_id": "2 220 c b"}
    ]

    await coordinator._async_refresh_static()

    delete.assert_called_once_with(
        coordinator.hass, "tfi_live", f"{mock_entry.entry_id}_8370B2418801_2 220 c b"
    )
    create.assert_not_called()


async def test_no_repairs_check_before_cache_available(
    coordinator, mock_entry, mock_cache, mock_ir
):
    """No issue is created or deleted before the cache has loaded once."""
    create, delete, _ = mock_ir
    mock_cache.available = False
    mock_entry.data["sensors"] = [
        {"name": "Farranlea Pk 220", "stop_id": "8370B2418801", "route_id": "220"}
    ]

    await coordinator._async_refresh_static()

    create.assert_not_called()
    delete.assert_not_called()


async def test_shared_pair_produces_one_issue_naming_both_sensors(
    coordinator, mock_entry, mock_cache, mock_ir
):
    """Two sensors sharing a (stop_id, route_id) pair raise exactly one issue."""
    create, delete, _ = mock_ir
    mock_cache.available = True
    mock_cache.has_scheduled_pair = MagicMock(return_value=False)
    mock_entry.data["sensors"] = [
        {
            "name": "Inbound",
            "stop_id": "8370B2418801",
            "route_id": "220",
            "direction_id": 0,
        },
        {
            "name": "Outbound",
            "stop_id": "8370B2418801",
            "route_id": "220",
            "direction_id": 1,
        },
    ]

    await coordinator._async_refresh_static()

    create.assert_called_once()
    _, kwargs = create.call_args
    sensor_names = kwargs["translation_placeholders"]["sensor_names"]
    assert "Inbound" in sensor_names
    assert "Outbound" in sensor_names


async def test_no_repairs_check_on_static_refresh_failure(
    coordinator, mock_entry, mock_cache, mock_ir
):
    """A failed static refresh never evaluates Repairs issues."""
    create, delete, _ = mock_ir
    mock_cache.async_refresh_if_stale = AsyncMock(
        side_effect=StaticGtfsLoadError("HTTP 500")
    )
    mock_cache.available = True
    mock_entry.data["sensors"] = [
        {"name": "Farranlea Pk 220", "stop_id": "8370B2418801", "route_id": "220"}
    ]

    await coordinator._async_refresh_static()

    create.assert_not_called()
    delete.assert_not_called()


async def test_stale_issue_cleared_when_sensor_pair_no_longer_configured(
    coordinator, mock_entry, mock_cache, mock_ir
):
    """An issue for a pair edited/removed from config is cleared even though
    it never re-enters the per-pair loop.

    Simulates a previously-raised issue (e.g. from a since-edited or
    since-deleted sensor) still sitting in the issue registry, with no
    sensor in the current config referencing that pair any more.
    """
    create, delete, get_registry = mock_ir
    mock_cache.available = True
    mock_cache.has_scheduled_pair = MagicMock(return_value=True)
    mock_entry.data["sensors"] = [
        {"name": "Current Sensor", "stop_id": "STOP_NEW", "route_id": "ROUTE_NEW"}
    ]
    stale_issue_id = f"{mock_entry.entry_id}_STOP_OLD_ROUTE_OLD"
    get_registry.return_value.issues = {
        ("tfi_live", stale_issue_id): MagicMock(),
        ("other_domain", "unrelated_issue"): MagicMock(),
    }

    await coordinator._async_refresh_static()

    delete.assert_any_call(coordinator.hass, "tfi_live", stale_issue_id)
    assert delete.call_count == 2  # the matched current pair, plus the stale one


async def test_current_unmatched_pair_not_cleared_as_stale(
    coordinator, mock_entry, mock_cache, mock_ir
):
    """A pair that is both currently unmatched and already in the registry
    keeps its issue — the stale-cleanup pass must not delete it."""
    create, delete, get_registry = mock_ir
    mock_cache.available = True
    mock_cache.has_scheduled_pair = MagicMock(return_value=False)
    mock_entry.data["sensors"] = [
        {"name": "Farranlea Pk 220", "stop_id": "8370B2418801", "route_id": "220"}
    ]
    issue_id = f"{mock_entry.entry_id}_8370B2418801_220"
    get_registry.return_value.issues = {("tfi_live", issue_id): MagicMock()}

    await coordinator._async_refresh_static()

    create.assert_called_once()
    delete.assert_not_called()
