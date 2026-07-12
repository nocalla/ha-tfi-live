"""Tests for TfiLiveSensor — state, attributes, and availability.

Covers entity creation, state derivation (including truncation of fractional
and negative minutes), the departures attribute, availability rules, config
passthrough attributes, and unique-ID stability and distinctness.

Each test is self-contained.  The coordinator and static cache are replaced
by lightweight ``MagicMock`` instances; no live HA instance is required.
"""

from datetime import UTC, date, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from homeassistant.components.sensor import SensorEntity

from custom_components.ha_tfi_live.const import (
    ATTR_DEPARTURES,
    ATTR_DIRECTION_ID,
    ATTR_LAST_UPDATED,
    ATTR_OPERATOR_ID,
    ATTR_ROUTE_ID,
    ATTR_STOP_ID,
    DEP_DELAY_MINUTES,
    DEP_REALTIME_TIME,
    DEP_ROUTE_NAME,
    DEP_SCHEDULED_TIME,
    DEP_TRIP_ID,
)
from custom_components.ha_tfi_live.sensor import (
    TfiLiveSensor,
    _now_dublin,
    _parse_hhmm_today,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_coordinator():
    """Return a minimal mocked coordinator with healthy defaults."""
    coord = MagicMock()
    coord.last_update_success = True
    coord.last_successful_fetch = datetime.now(UTC)
    coord.data = {"entities": []}
    coord._cache = MagicMock()
    coord.cache = coord._cache
    coord._cache.get_scheduled_departures.return_value = []
    return coord


@pytest.fixture
def sensor_config():
    """Return a base sensor configuration dict."""
    return {
        "name": "Next 46A",
        "stop_id": "STOP_A",
        "route_id": "46A",
        "direction_id": None,
        "operator_id": None,
    }


@pytest.fixture
def sensor(mock_coordinator, sensor_config):
    """Return a TfiLiveSensor wired to the mocked coordinator."""
    return TfiLiveSensor(mock_coordinator, sensor_config, "entry_123")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_unix_ts(minutes_from_now: float) -> int:
    """Return a Unix timestamp offset from the current time.

    Args:
        minutes_from_now: Positive values are in the future; negative in the
            past.

    Returns:
        Integer Unix timestamp.
    """
    return int((datetime.now() + timedelta(minutes=minutes_from_now)).timestamp())


def make_rt_entity(
    trip_id: str,
    route_id: str = "46A",
    stop_id: str = "STOP_A",
    direction_id=None,
    minutes_from_now: float = 5.0,
    delay_secs: int = 0,
) -> dict:
    """Build a minimal RT entity dict as the coordinator would produce.

    Args:
        trip_id: GTFS trip identifier.
        route_id: GTFS route identifier.
        stop_id: GTFS stop identifier.
        direction_id: Integer direction or None.
        minutes_from_now: Arrival/departure offset from now in minutes.
        delay_secs: Delay in seconds applied to both arrival and departure.

    Returns:
        Dict matching the shape expected by ``TfiLiveSensor._get_departures``.
    """
    ts = make_unix_ts(minutes_from_now)
    return {
        "trip_id": trip_id,
        "route_id": route_id,
        "direction_id": str(direction_id) if direction_id is not None else None,
        "start_date": "20260517",
        "stop_time_updates": [
            {
                "stop_id": stop_id,
                "arrival_delay": delay_secs,
                "departure_delay": delay_secs,
                "arrival_time": ts,
                "departure_time": ts,
            }
        ],
    }


# ---------------------------------------------------------------------------
# Entity creation: sensor count
# ---------------------------------------------------------------------------


def test_entity_count(mock_coordinator):
    """Two TfiLiveSensor instances can be constructed from two configs."""
    # Arrange
    cfg_a = {
        "name": "Stop A",
        "stop_id": "STOP_A",
        "route_id": "46A",
        "direction_id": None,
        "operator_id": None,
    }
    cfg_b = {
        "name": "Stop B",
        "stop_id": "STOP_B",
        "route_id": "39A",
        "direction_id": None,
        "operator_id": None,
    }

    # Act
    sensor_a = TfiLiveSensor(mock_coordinator, cfg_a, "entry_1")
    sensor_b = TfiLiveSensor(mock_coordinator, cfg_b, "entry_1")

    # Assert
    assert isinstance(sensor_a, TfiLiveSensor)
    assert isinstance(sensor_b, TfiLiveSensor)


# ---------------------------------------------------------------------------
# No device_tracker; sensor is a SensorEntity
# ---------------------------------------------------------------------------


def test_no_device_tracker(sensor):
    """TfiLiveSensor is a SensorEntity and not a device_tracker."""
    assert isinstance(sensor, SensorEntity)


# ---------------------------------------------------------------------------
# State: floor of fractional positive minutes
# ---------------------------------------------------------------------------


def test_native_value_floor_positive(mock_coordinator, sensor_config):
    """native_value is floor(2.9) == 2 for a departure 2.9 min away."""
    # Arrange
    mock_coordinator.data = {"entities": [make_rt_entity("T1", minutes_from_now=2.9)]}
    s = TfiLiveSensor(mock_coordinator, sensor_config, "entry_123")

    # Act
    value = s.native_value

    # Assert
    assert value == 2


# ---------------------------------------------------------------------------
# State: floor of fractional negative minutes
# ---------------------------------------------------------------------------


def test_native_value_floor_negative(mock_coordinator, sensor_config):
    """native_value is -1 for T = -1.3.

    Negative minutes must truncate toward zero (``int(-1.3) == -1``), not
    round down like ``math.floor(-1.3) == -2``.  This test will fail if the
    implementation uses ``math.floor`` rather than truncation for the
    negative case.
    """
    # Arrange — departure is slightly overdue but within the 5-min grace window
    mock_coordinator.data = {"entities": [make_rt_entity("T1", minutes_from_now=-1.3)]}
    s = TfiLiveSensor(mock_coordinator, sensor_config, "entry_123")

    # Act
    value = s.native_value

    # Assert — truncation toward zero: -1.3 -> -1
    assert value == -1


# ---------------------------------------------------------------------------
# State: negative value when departure 2 min 45 sec in the past
# ---------------------------------------------------------------------------


def test_native_value_overdue_negative_2(mock_coordinator, sensor_config):
    """native_value == -2 when departure was 2m45s ago.

    T = -2.75 must truncate toward zero to ``-2`` rather than round down via
    ``math.floor`` (which would give ``-3``).  This test will fail if the
    implementation rounds in the wrong direction.
    """
    # Arrange — 2 min 45 sec ago = -2.75 minutes
    mock_coordinator.data = {"entities": [make_rt_entity("T1", minutes_from_now=-2.75)]}
    s = TfiLiveSensor(mock_coordinator, sensor_config, "entry_123")

    # Act
    value = s.native_value

    # Assert — truncation toward zero, not math.floor: -2.75 -> -2
    assert value == -2


# ---------------------------------------------------------------------------
# State: scheduled fallback when no RT data exists
# ---------------------------------------------------------------------------


def test_native_value_scheduled_fallback_when_no_rt(mock_coordinator, sensor_config):
    """When no RT entity exists, native_value uses scheduled time.

    The departure dict for a static-only trip must have realtime_time == None.
    """
    # Arrange — no RT entities; one static departure 10 min from now
    mock_coordinator.data = {"entities": []}
    future = _now_dublin() + timedelta(minutes=10)
    hhmm = future.strftime("%H:%M")
    mock_coordinator._cache.get_scheduled_departures.return_value = [
        ("TRIP_X", hhmm, "46A")
    ]
    s = TfiLiveSensor(mock_coordinator, sensor_config, "entry_123")

    # Act
    value = s.native_value
    attrs = s.extra_state_attributes

    # Assert
    assert value is not None
    assert isinstance(value, int)
    # Should be approximately 10 minutes (floor)
    assert 8 <= value <= 10
    assert attrs[ATTR_DEPARTURES][0][DEP_REALTIME_TIME] is None


# ---------------------------------------------------------------------------
# Departures attribute: at most 3 entries when 5 RT entities match
# ---------------------------------------------------------------------------


def test_departures_attribute_at_most_3(mock_coordinator, sensor_config):
    """Given 5 matching RT departures, departures has exactly 3 entries."""
    # Arrange — 5 RT entities each 1 min apart starting at 5 min from now
    entities = [make_rt_entity(f"T{i}", minutes_from_now=5.0 + i) for i in range(1, 6)]
    mock_coordinator.data = {"entities": entities}
    s = TfiLiveSensor(mock_coordinator, sensor_config, "entry_123")

    # Act
    attrs = s.extra_state_attributes

    # Assert
    assert len(attrs[ATTR_DEPARTURES]) == 3


# ---------------------------------------------------------------------------
# Departures attribute: exact keys, no extras
# ---------------------------------------------------------------------------


def test_departures_attribute_exact_keys(mock_coordinator, sensor_config):
    """Each departure dict has exactly the 5 specified keys and no others."""
    # Arrange
    mock_coordinator.data = {"entities": [make_rt_entity("T1", minutes_from_now=5.0)]}
    s = TfiLiveSensor(mock_coordinator, sensor_config, "entry_123")
    expected_keys = {
        DEP_SCHEDULED_TIME,
        DEP_REALTIME_TIME,
        DEP_DELAY_MINUTES,
        DEP_TRIP_ID,
        DEP_ROUTE_NAME,
    }

    # Act
    attrs = s.extra_state_attributes
    departure = attrs[ATTR_DEPARTURES][0]

    # Assert
    assert set(departure.keys()) == expected_keys


# ---------------------------------------------------------------------------
# Departures attribute: fewer than 3 when only 1 departure exists
# ---------------------------------------------------------------------------


def test_departures_fewer_than_3(mock_coordinator, sensor_config):
    """Given 1 matching RT departure, departures contains exactly 1 entry."""
    # Arrange
    mock_coordinator.data = {"entities": [make_rt_entity("T1", minutes_from_now=5.0)]}
    s = TfiLiveSensor(mock_coordinator, sensor_config, "entry_123")

    # Act
    attrs = s.extra_state_attributes

    # Assert
    assert len(attrs[ATTR_DEPARTURES]) == 1


# ---------------------------------------------------------------------------
# Departures attribute: ascending sort order (RT before static)
# ---------------------------------------------------------------------------


def test_departures_sort_order(mock_coordinator, sensor_config):
    """Departures are sorted [B-RT@5min, C-RT@8min, A-static@10min]."""
    # Arrange
    future_10 = _now_dublin() + timedelta(minutes=10)
    hhmm_10min = future_10.strftime("%H:%M")

    mock_coordinator._cache.get_scheduled_departures.return_value = [
        ("T_A", hhmm_10min, "46A")
    ]
    mock_coordinator.data = {
        "entities": [
            make_rt_entity("T_B", minutes_from_now=5.0),
            make_rt_entity("T_C", minutes_from_now=8.0),
        ]
    }
    s = TfiLiveSensor(mock_coordinator, sensor_config, "entry_123")

    # Act
    attrs = s.extra_state_attributes
    departures = attrs[ATTR_DEPARTURES]

    # Assert
    assert departures[0][DEP_TRIP_ID] == "T_B"
    assert departures[1][DEP_TRIP_ID] == "T_C"
    assert departures[2][DEP_TRIP_ID] == "T_A"


# ---------------------------------------------------------------------------
# No departures: state None, departures empty, still available
# ---------------------------------------------------------------------------


def test_no_departures_state_none_available_true(mock_coordinator, sensor_config):
    """With no RT and no static, native_value is None and available is True."""
    # Arrange — everything empty
    mock_coordinator.data = {"entities": []}
    mock_coordinator._cache.get_scheduled_departures.return_value = []
    s = TfiLiveSensor(mock_coordinator, sensor_config, "entry_123")

    # Act / Assert
    assert s.native_value is None
    assert s.available is True
    assert s.extra_state_attributes[ATTR_DEPARTURES] == []


# ---------------------------------------------------------------------------
# Static unavailable: RT data still used; scheduled_time/route_name None
# ---------------------------------------------------------------------------


def test_static_unavailable_graceful(mock_coordinator, sensor_config):
    """With static returning [], RT departure has scheduled_time None."""
    # Arrange — one RT entity; static returns nothing
    mock_coordinator.data = {"entities": [make_rt_entity("T1", minutes_from_now=5.0)]}
    mock_coordinator._cache.get_scheduled_departures.return_value = []
    s = TfiLiveSensor(mock_coordinator, sensor_config, "entry_123")

    # Act
    attrs = s.extra_state_attributes

    # Assert
    assert s.available is True
    dep = attrs[ATTR_DEPARTURES][0]
    assert dep[DEP_SCHEDULED_TIME] is None
    assert dep[DEP_ROUTE_NAME] is None
    # RT time is still present
    assert dep[DEP_REALTIME_TIME] is not None


# ---------------------------------------------------------------------------
# Availability: False when last fetch is too old
# ---------------------------------------------------------------------------


def test_unavailable_when_fetch_old(mock_coordinator, sensor_config):
    """available is False when _last_successful_fetch > 3 minutes ago."""
    # Arrange — last fetch was 200 seconds ago (> 180 s window)
    mock_coordinator.last_successful_fetch = datetime.now(UTC) - timedelta(seconds=200)
    s = TfiLiveSensor(mock_coordinator, sensor_config, "entry_123")

    # Assert
    assert s.available is False


# ---------------------------------------------------------------------------
# Unavailable: all attribute values are None
# ---------------------------------------------------------------------------


def test_attributes_all_none_when_unavailable(mock_coordinator, sensor_config):
    """When unavailable, all extra_state_attributes values are None."""
    # Arrange
    mock_coordinator.last_successful_fetch = datetime.now(UTC) - timedelta(seconds=200)
    s = TfiLiveSensor(mock_coordinator, sensor_config, "entry_123")

    # Act
    attrs = s.extra_state_attributes

    # Assert
    assert s.available is False
    assert s.native_value is None
    for key, val in attrs.items():
        assert val is None, f"Expected {key} to be None but got {val!r}"


# ---------------------------------------------------------------------------
# Availability: True when last fetch is recent
# ---------------------------------------------------------------------------


def test_available_when_fetch_recent(mock_coordinator, sensor_config):
    """available is True when _last_successful_fetch is within 3 minutes."""
    # Arrange — just updated
    mock_coordinator.last_successful_fetch = datetime.now(UTC)
    s = TfiLiveSensor(mock_coordinator, sensor_config, "entry_123")

    # Assert
    assert s.available is True


# ---------------------------------------------------------------------------
# Config values present in attributes
# ---------------------------------------------------------------------------


def test_config_values_in_attributes(mock_coordinator):
    """stop_id, route_id, direction_id, operator_id match config exactly."""
    # Arrange
    cfg = {
        "name": "Test Sensor",
        "stop_id": "STOP_A",
        "route_id": "46A",
        "direction_id": None,
        "operator_id": None,
    }
    s = TfiLiveSensor(mock_coordinator, cfg, "entry_123")

    # Act
    attrs = s.extra_state_attributes

    # Assert
    assert attrs[ATTR_STOP_ID] == "STOP_A"
    assert attrs[ATTR_ROUTE_ID] == "46A"
    assert attrs[ATTR_DIRECTION_ID] is None
    assert attrs[ATTR_OPERATOR_ID] is None


# ---------------------------------------------------------------------------
# last_updated is an ISO 8601 string starting with the correct date
# ---------------------------------------------------------------------------


def test_last_updated_iso8601(mock_coordinator, sensor_config):
    """last_updated attribute is an ISO 8601 string of the fetch time.

    The timestamp must be recent enough to keep the sensor available, so we
    use a time 10 seconds ago.  We verify the format by round-tripping through
    ``datetime.fromisoformat`` and confirming the date portion is correct.
    """
    # Arrange — use a timestamp that keeps the sensor available (<180 s ago)
    fetch_time = datetime.now(UTC) - timedelta(seconds=10)
    mock_coordinator.last_successful_fetch = fetch_time
    s = TfiLiveSensor(mock_coordinator, sensor_config, "entry_123")

    # Act
    attrs = s.extra_state_attributes

    # Assert — sensor must be available for last_updated to be non-None
    assert s.available is True
    last_updated = attrs[ATTR_LAST_UPDATED]
    assert isinstance(last_updated, str)
    # Must be parseable as ISO 8601
    parsed = datetime.fromisoformat(last_updated)
    assert parsed.date() == fetch_time.date()


# ---------------------------------------------------------------------------
# Unique ID — stability and distinctness
# ---------------------------------------------------------------------------


def test_unique_id_stable(mock_coordinator):
    """The same config always produces the same unique_id."""
    cfg = {
        "name": "Sensor",
        "stop_id": "STOP_A",
        "route_id": "46A",
        "direction_id": None,
        "operator_id": None,
    }
    s1 = TfiLiveSensor(mock_coordinator, cfg, "entry_xyz")
    s2 = TfiLiveSensor(mock_coordinator, cfg, "entry_xyz")

    assert s1.unique_id == s2.unique_id


def test_unique_id_distinct_direction(mock_coordinator):
    """Sensors with different direction_id values have different unique_ids."""
    base = {
        "name": "Sensor",
        "stop_id": "STOP_A",
        "route_id": "46A",
        "operator_id": None,
    }
    cfg_0 = {**base, "direction_id": 0}
    cfg_1 = {**base, "direction_id": 1}

    s0 = TfiLiveSensor(mock_coordinator, cfg_0, "entry_xyz")
    s1 = TfiLiveSensor(mock_coordinator, cfg_1, "entry_xyz")

    assert s0.unique_id != s1.unique_id


def test_unique_id_distinct_operator(mock_coordinator):
    """Sensors differing only in operator_id have different unique_ids."""
    base = {
        "name": "Sensor",
        "stop_id": "STOP_A",
        "route_id": "46A",
        "direction_id": None,
    }
    cfg_nta = {**base, "operator_id": "NTA"}
    cfg_none = {**base, "operator_id": None}

    s_nta = TfiLiveSensor(mock_coordinator, cfg_nta, "entry_xyz")
    s_none = TfiLiveSensor(mock_coordinator, cfg_none, "entry_xyz")

    assert s_nta.unique_id != s_none.unique_id


# ---------------------------------------------------------------------------
# _parse_hhmm_today — unit tests
# ---------------------------------------------------------------------------


def test_parse_hhmm_today_returns_correct_time():
    """_parse_hhmm_today returns a datetime with the expected hour and minute."""
    # Arrange / Act
    result = _parse_hhmm_today("09:35")

    # Assert
    assert result.hour == 9
    assert result.minute == 35
    assert result.date() == date.today()


def test_parse_hhmm_today_midnight():
    """_parse_hhmm_today handles midnight (00:00) correctly."""
    result = _parse_hhmm_today("00:00")
    assert result.hour == 0
    assert result.minute == 0


def test_parse_hhmm_today_last_minute_of_day():
    """_parse_hhmm_today handles 23:59 correctly."""
    result = _parse_hhmm_today("23:59")
    assert result.hour == 23
    assert result.minute == 59


# ---------------------------------------------------------------------------
# Branch coverage: uncovered paths (Issue #31 — enforce >= 95% coverage)
# ---------------------------------------------------------------------------


def test_name_property_returns_configured_name(mock_coordinator, sensor_config):
    """name property returns the configured sensor name."""
    sensor_config["name"] = "My Bus Stop"
    s = TfiLiveSensor(mock_coordinator, sensor_config, "e1")
    assert s.name == "My Bus Stop"


def test_native_unit_of_measurement(mock_coordinator, sensor_config):
    """native_unit_of_measurement returns 'min'."""
    s = TfiLiveSensor(mock_coordinator, sensor_config, "e1")
    assert s.native_unit_of_measurement == "min"


def test_device_class_is_none(mock_coordinator, sensor_config):
    """device_class returns None (negative values disqualify DURATION class)."""
    s = TfiLiveSensor(mock_coordinator, sensor_config, "e1")
    assert s.device_class is None


def test_available_false_when_last_update_success_false(
    mock_coordinator, sensor_config
):
    """available returns False when coordinator.last_update_success is False."""
    mock_coordinator.last_update_success = False
    s = TfiLiveSensor(mock_coordinator, sensor_config, "e1")
    assert s.available is False


def test_available_false_when_last_successful_fetch_none(
    mock_coordinator, sensor_config
):
    """available returns False when last_successful_fetch is None."""
    mock_coordinator.last_update_success = True
    mock_coordinator.last_successful_fetch = None
    s = TfiLiveSensor(mock_coordinator, sensor_config, "e1")
    assert s.available is False


def test_get_departures_returns_empty_when_coordinator_data_falsy(
    mock_coordinator, sensor_config
):
    """_get_departures returns [] when coordinator.data is None or empty."""
    mock_coordinator.data = None
    s = TfiLiveSensor(mock_coordinator, sensor_config, "e1")
    assert s._get_departures() == []


def test_get_departures_skips_wrong_route_id(mock_coordinator, sensor_config):
    """RT entity with non-matching route_id is ignored."""
    mock_coordinator.data = {
        "entities": [
            {
                "trip_id": "T1",
                "route_id": "39A",  # different from sensor's 46A
                "direction_id": None,
                "start_date": "20260517",
                "stop_time_updates": [
                    {
                        "stop_id": "STOP_A",
                        "arrival_time": 9999999999,
                        "departure_time": 9999999999,
                        "arrival_delay": 0,
                        "departure_delay": 0,
                    }
                ],
            }
        ]
    }
    s = TfiLiveSensor(mock_coordinator, sensor_config, "e1")
    assert s._get_departures() == []


def test_get_departures_skips_wrong_direction(mock_coordinator):
    """RT entity filtered out when direction_id doesn't match."""
    cfg = {
        "name": "Sensor",
        "stop_id": "STOP_A",
        "route_id": "46A",
        "direction_id": 1,  # filter for direction 1
        "operator_id": None,
    }
    mock_coordinator.data = {
        "entities": [
            {
                "trip_id": "T1",
                "route_id": "46A",
                "direction_id": "0",  # entity is direction 0
                "start_date": "20260517",
                "stop_time_updates": [
                    {
                        "stop_id": "STOP_A",
                        "arrival_time": 9999999999,
                        "departure_time": 9999999999,
                        "arrival_delay": 0,
                        "departure_delay": 0,
                    }
                ],
            }
        ]
    }
    s = TfiLiveSensor(mock_coordinator, cfg, "e1")
    assert s._get_departures() == []


def test_get_departures_skips_wrong_stop_id(mock_coordinator, sensor_config):
    """stop_time_update with non-matching stop_id is skipped."""
    from datetime import timedelta

    future_ts = int((datetime.now() + timedelta(minutes=5)).timestamp())
    mock_coordinator.data = {
        "entities": [
            {
                "trip_id": "T1",
                "route_id": "46A",
                "direction_id": None,
                "start_date": "20260517",
                "stop_time_updates": [
                    {
                        "stop_id": "STOP_B",  # wrong stop
                        "arrival_time": future_ts,
                        "departure_time": future_ts,
                        "arrival_delay": 0,
                        "departure_delay": 0,
                    }
                ],
            }
        ]
    }
    s = TfiLiveSensor(mock_coordinator, sensor_config, "e1")
    assert s._get_departures() == []


def test_get_departures_skips_null_unix_ts(mock_coordinator, sensor_config):
    """stop_time_update with None departure_time and None arrival_time is skipped."""
    mock_coordinator.data = {
        "entities": [
            {
                "trip_id": "T1",
                "route_id": "46A",
                "direction_id": None,
                "start_date": "20260517",
                "stop_time_updates": [
                    {
                        "stop_id": "STOP_A",
                        "arrival_time": None,
                        "departure_time": None,
                        "arrival_delay": None,
                        "departure_delay": None,
                    }
                ],
            }
        ]
    }
    s = TfiLiveSensor(mock_coordinator, sensor_config, "e1")
    assert s._get_departures() == []


def test_get_departures_rt_trip_enriched_from_static(mock_coordinator, sensor_config):
    """RT departure is enriched with sched_time and route_name from static data."""
    from datetime import timedelta

    future_ts = int((datetime.now() + timedelta(minutes=10)).timestamp())
    mock_coordinator.data = {
        "entities": [
            {
                "trip_id": "TRIP_A",
                "route_id": "46A",
                "direction_id": None,
                "start_date": "20260517",
                "stop_time_updates": [
                    {
                        "stop_id": "STOP_A",
                        "arrival_time": future_ts,
                        "departure_time": future_ts,
                        "arrival_delay": 0,
                        "departure_delay": 60,
                    }
                ],
            }
        ]
    }
    # Return TRIP_A in static data too
    future = datetime.now() + timedelta(minutes=9)
    mock_coordinator._cache.get_scheduled_departures.return_value = [
        ("TRIP_A", future.strftime("%H:%M"), "46A Route Name")
    ]
    s = TfiLiveSensor(mock_coordinator, sensor_config, "e1")
    deps = s._get_departures()
    assert len(deps) == 1
    dep = deps[0]
    assert dep["scheduled_time"] == future.strftime("%H:%M")
    assert dep["route_name"] == "46A Route Name"
    assert dep["realtime_time"] is not None


def test_get_departures_static_only_trip_skipped_if_already_in_rt(
    mock_coordinator, sensor_config
):
    """Static departure for a trip already in RT is not double-counted."""
    from datetime import timedelta

    future_ts = int((datetime.now() + timedelta(minutes=5)).timestamp())
    mock_coordinator.data = {
        "entities": [
            {
                "trip_id": "TRIP_A",
                "route_id": "46A",
                "direction_id": None,
                "start_date": "20260517",
                "stop_time_updates": [
                    {
                        "stop_id": "STOP_A",
                        "arrival_time": future_ts,
                        "departure_time": future_ts,
                        "arrival_delay": 0,
                        "departure_delay": 0,
                    }
                ],
            }
        ]
    }
    # Static also lists TRIP_A — should be skipped (already in RT)
    future = datetime.now() + timedelta(minutes=5)
    mock_coordinator._cache.get_scheduled_departures.return_value = [
        ("TRIP_A", future.strftime("%H:%M"), "46A")
    ]
    s = TfiLiveSensor(mock_coordinator, sensor_config, "e1")
    deps = s._get_departures()
    # Should only have 1 departure, not 2
    assert len(deps) == 1
    assert deps[0]["realtime_time"] is not None


# ---------------------------------------------------------------------------
# async_setup_entry: direct call coverage (lines 57-62)
# ---------------------------------------------------------------------------


async def test_sensor_async_setup_entry_registers_entities() -> None:
    """sensor.async_setup_entry creates TfiLiveSensor entities from entry.runtime_data.

    Calls the sensor platform async_setup_entry directly with a mock entry
    and mock async_add_entities to exercise the function body.

    Arrange: mock coordinator on entry.runtime_data; two sensor configs in
        entry.data[CONF_SENSORS]; mock async_add_entities callback.
    Act: call sensor.async_setup_entry.
    Assert: async_add_entities was called once with a list of two TfiLiveSensors.
    """
    from unittest.mock import MagicMock

    from custom_components.ha_tfi_live.const import CONF_SENSORS
    from custom_components.ha_tfi_live.sensor import (
        TfiLiveSensor,
        async_setup_entry,
    )

    coord = MagicMock()
    coord.last_update_success = True
    coord._last_successful_fetch = None
    coord.data = {"entities": []}
    coord._cache = MagicMock()
    coord.cache = coord._cache
    coord._cache.get_scheduled_departures.return_value = []

    entry = MagicMock()
    entry.entry_id = "eid"
    entry.runtime_data = coord
    entry.data = {
        CONF_SENSORS: [
            {"stop_id": "S1", "route_id": "46A", "name": "Sensor 1"},
            {"stop_id": "S2", "route_id": "39A", "name": "Sensor 2"},
        ]
    }

    hass = MagicMock()
    added = []

    def fake_add(entities, _update):
        """Capture entities passed to async_add_entities."""
        added.extend(entities)

    await async_setup_entry(hass, entry, fake_add)

    assert len(added) == 2
    assert all(isinstance(e, TfiLiveSensor) for e in added)
    assert added[0]._stop_id == "S1"
    assert added[1]._stop_id == "S2"
