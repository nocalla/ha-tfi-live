"""Tests for custom_components.ha_tfi_live.config_flow.

Covers re-auth preserving config, step 1 required field and URL validation,
step 2 required field and direction_id validation, and repeated sensor
addition.

Also covers issue #27 (API probe during step 1), issue #34 (reconfigure flow
and options flow).

All tests use pytest-asyncio (asyncio_mode = "auto") and unittest.mock only.
No live network calls are made; any HTTP call attempted in the flow raises an
AssertionError via a sentinel patch so the test fails loudly if the production
code ever regresses on the no-network constraint.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from homeassistant.data_entry_flow import AbortFlow, FlowResultType

from custom_components.ha_tfi_live.config_flow import (
    TfiLiveConfigFlow,
    TfiLiveOptionsFlowHandler,
)
from custom_components.ha_tfi_live.const import (
    CONF_API_KEY,
    CONF_DIRECTION_ID,
    CONF_ROUTE_ID,
    CONF_SENSORS,
    CONF_STATIC_GTFS_URL,
    CONF_STOP_ID,
    CONF_TRIP_UPDATE_URL,
    DEFAULT_STATIC_GTFS_URL,
    DEFAULT_TRIP_UPDATE_URL,
)

# ---------------------------------------------------------------------------
# Shared test data
# ---------------------------------------------------------------------------

VALID_STEP1 = {
    CONF_API_KEY: "my-api-key",
    CONF_TRIP_UPDATE_URL: (
        "https://gtfsr.transportforireland.ie/v2/TripUpdates?format=json"
    ),
    CONF_STATIC_GTFS_URL: (
        "https://www.transportforireland.ie/transitData/Data/GTFS_Realtime.zip"
    ),
}

VALID_STEP2 = {
    "name": "Next 46A",
    CONF_STOP_ID: "STOP_A",
    CONF_ROUTE_ID: "46A",
    CONF_DIRECTION_ID: "",
    "operator_id": "",
}

_PREFILLED_CONFIG = {
    CONF_API_KEY: "k",
    CONF_TRIP_UPDATE_URL: "https://a.com",
    CONF_STATIC_GTFS_URL: "https://b.com",
    CONF_SENSORS: [],
}

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_hass() -> MagicMock:
    """Return a minimal MagicMock that satisfies the flow's hass usage."""
    hass = MagicMock()
    hass.config_entries = MagicMock()
    hass.config_entries.async_update_entry = MagicMock()
    hass.config_entries.async_reload = AsyncMock()
    return hass


@pytest.fixture
def flow(mock_hass: MagicMock) -> TfiLiveConfigFlow:
    """Return a TfiLiveConfigFlow wired up with stub base-class methods.

    The HA ConfigFlow base class methods that normally require a running HA
    instance (async_show_form, async_show_menu, async_create_entry,
    async_abort, async_set_unique_id, _abort_if_unique_id_configured) are
    replaced with lightweight stubs that return dicts matching the
    FlowResultType contract, or are no-ops for the duplicate-guard helpers
    (simulating "no existing entry" by default).
    """
    f = TfiLiveConfigFlow()
    f.hass = mock_hass
    f.context = {}

    f.async_show_form = lambda **kwargs: {
        "type": FlowResultType.FORM,
        "step_id": kwargs.get("step_id"),
        "errors": kwargs.get("errors", {}),
        "data_schema": kwargs.get("data_schema"),
    }
    f.async_show_menu = lambda **kwargs: {
        "type": FlowResultType.MENU,
        "step_id": kwargs.get("step_id"),
        "menu_options": kwargs.get("menu_options", []),
    }
    f.async_create_entry = lambda title, data: {
        "type": FlowResultType.CREATE_ENTRY,
        "title": title,
        "data": data,
    }
    f.async_abort = lambda reason: {
        "type": FlowResultType.ABORT,
        "reason": reason,
    }

    # Default stubs for the duplicate-entry guard: no-op (no existing entry).
    # Tests that exercise the duplicate path override these on the instance.
    async def _noop_set_unique_id(unique_id: str) -> None:
        """Stub: no-op when no existing entry is present."""

    f.async_set_unique_id = _noop_set_unique_id  # type: ignore[method-assign]
    f._abort_if_unique_id_configured = lambda: None  # type: ignore[method-assign]

    return f


# ---------------------------------------------------------------------------
# Step 1 — required field validation (api_key)
# ---------------------------------------------------------------------------


async def test_step1_empty_api_key_returns_error(flow: TfiLiveConfigFlow) -> None:
    """Step 1 with api_key='' returns FORM and errors['api_key']=='required'."""
    # Arrange
    user_input = {**VALID_STEP1, CONF_API_KEY: ""}

    # Act
    result = await flow.async_step_user(user_input)

    # Assert
    assert result["type"] == FlowResultType.FORM
    assert result["errors"][CONF_API_KEY] == "required"


async def test_step1_whitespace_api_key_returns_error(
    flow: TfiLiveConfigFlow,
) -> None:
    """Step 1 with api_key='   ' (whitespace only) is treated as empty."""
    # Arrange
    user_input = {**VALID_STEP1, CONF_API_KEY: "   "}

    # Act
    result = await flow.async_step_user(user_input)

    # Assert
    assert result["type"] == FlowResultType.FORM
    assert result["errors"][CONF_API_KEY] == "required"


# ---------------------------------------------------------------------------
# Step 1 — URL validation
# ---------------------------------------------------------------------------


async def test_step1_invalid_trip_url(flow: TfiLiveConfigFlow) -> None:
    """Step 1 with trip_update_url='not-a-url' returns invalid_url error."""
    # Arrange
    user_input = {**VALID_STEP1, CONF_TRIP_UPDATE_URL: "not-a-url"}

    # Act
    result = await flow.async_step_user(user_input)

    # Assert
    assert result["type"] == FlowResultType.FORM
    assert result["errors"][CONF_TRIP_UPDATE_URL] == "invalid_url"


async def test_step1_invalid_static_url(flow: TfiLiveConfigFlow) -> None:
    """Step 1 with static_gtfs_url='not-a-url' returns invalid_url error."""
    # Arrange
    user_input = {**VALID_STEP1, CONF_STATIC_GTFS_URL: "not-a-url"}

    # Act
    result = await flow.async_step_user(user_input)

    # Assert
    assert result["type"] == FlowResultType.FORM
    assert result["errors"][CONF_STATIC_GTFS_URL] == "invalid_url"


def _make_mock_session(status: int = 200) -> MagicMock:
    """Build a mock aiohttp ClientSession whose GET returns the given HTTP status.

    Args:
        status: The HTTP status code the mock response should return.

    Returns:
        A MagicMock that replaces ``async_get_clientsession`` and whose
        ``session.get(...)`` async-context-manager yields a response with
        ``resp.status == status``.
    """
    mock_resp = MagicMock()
    mock_resp.status = status

    @asynccontextmanager
    async def _get(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        """Async context manager yielding the mock response."""
        yield mock_resp

    mock_session = MagicMock()
    mock_session.get = _get
    return mock_session


async def test_step1_valid_advances_to_sensor(flow: TfiLiveConfigFlow) -> None:
    """Valid step 1 input advances to the sensor form (step_id='sensor')."""
    # Arrange — mock the probe so no real HTTP call is made
    mock_session = _make_mock_session(status=200)
    with patch(
        "custom_components.ha_tfi_live.config_flow.async_get_clientsession",
        return_value=mock_session,
    ):
        result = await flow.async_step_user(VALID_STEP1)

    # Assert — step 1 calls async_step_sensor(None) which shows the sensor form
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "sensor"


async def test_step1_no_network_call(flow: TfiLiveConfigFlow) -> None:
    """Step 1 with validation errors must not make any HTTP calls."""

    def _raise(*args: object, **kwargs: object) -> None:
        raise AssertionError("HTTP call made during config flow step 1")

    # Patch both aiohttp.ClientSession and the stdlib urllib.request to catch
    # any network attempt.  Use invalid input so the flow stops at validation
    # before reaching the probe.
    with (
        patch("aiohttp.ClientSession", side_effect=_raise),
        patch("urllib.request.urlopen", side_effect=_raise),
    ):
        # Invalid URL — should not reach the probe.
        result = await flow.async_step_user(
            {**VALID_STEP1, CONF_TRIP_UPDATE_URL: "not-a-url"}
        )

    assert result["type"] == FlowResultType.FORM


# ---------------------------------------------------------------------------
# Issue #27 — API probe during step 1
# ---------------------------------------------------------------------------


async def test_step1_invalid_auth(flow: TfiLiveConfigFlow) -> None:
    """Issue #27: step 1 probe returning 401 re-shows form with invalid_auth error."""
    # Arrange
    mock_session = _make_mock_session(status=401)
    with patch(
        "custom_components.ha_tfi_live.config_flow.async_get_clientsession",
        return_value=mock_session,
    ):
        result = await flow.async_step_user(VALID_STEP1)

    # Assert
    assert result["type"] == FlowResultType.FORM
    assert result["errors"].get("base") == "invalid_auth"


async def test_step1_cannot_connect(flow: TfiLiveConfigFlow) -> None:
    """Issue #27: step 1 probe raising ClientError re-shows form with cannot_connect."""

    # Arrange — simulate a connection error from the probe
    @asynccontextmanager
    async def _raising_get(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        """Async context manager that raises aiohttp.ClientError."""
        raise aiohttp.ClientError("connection refused")
        yield  # make it a generator

    mock_session = MagicMock()
    mock_session.get = _raising_get

    with patch(
        "custom_components.ha_tfi_live.config_flow.async_get_clientsession",
        return_value=mock_session,
    ):
        result = await flow.async_step_user(VALID_STEP1)

    # Assert
    assert result["type"] == FlowResultType.FORM
    assert result["errors"].get("base") == "cannot_connect"


async def test_step1_http_500_returns_cannot_connect(flow: TfiLiveConfigFlow) -> None:
    """Issue #27: step 1 probe returning 5xx re-shows form with cannot_connect."""
    mock_session = _make_mock_session(status=500)
    with patch(
        "custom_components.ha_tfi_live.config_flow.async_get_clientsession",
        return_value=mock_session,
    ):
        result = await flow.async_step_user(VALID_STEP1)

    assert result["type"] == FlowResultType.FORM
    assert result["errors"].get("base") == "cannot_connect"


# ---------------------------------------------------------------------------
# Step 2 — required field validation
# ---------------------------------------------------------------------------


async def test_step2_empty_name_returns_error(flow: TfiLiveConfigFlow) -> None:
    """Step 2 with name='' returns FORM and errors['name']=='required'."""
    # Arrange
    flow._config = dict(_PREFILLED_CONFIG, **{CONF_SENSORS: []})
    user_input = {**VALID_STEP2, "name": ""}

    # Act
    result = await flow.async_step_sensor(user_input)

    # Assert
    assert result["type"] == FlowResultType.FORM
    assert result["errors"]["name"] == "required"


async def test_step2_empty_stop_id_returns_error(flow: TfiLiveConfigFlow) -> None:
    """Step 2 with stop_id='' returns FORM and errors['stop_id']=='required'."""
    # Arrange
    flow._config = dict(_PREFILLED_CONFIG, **{CONF_SENSORS: []})
    user_input = {**VALID_STEP2, CONF_STOP_ID: ""}

    # Act
    result = await flow.async_step_sensor(user_input)

    # Assert
    assert result["type"] == FlowResultType.FORM
    assert result["errors"][CONF_STOP_ID] == "required"


async def test_step2_empty_route_id_returns_error(flow: TfiLiveConfigFlow) -> None:
    """Empty route_id returns FORM with errors['route_id']=='required'."""
    # Arrange
    flow._config = dict(_PREFILLED_CONFIG, **{CONF_SENSORS: []})
    user_input = {**VALID_STEP2, CONF_ROUTE_ID: ""}

    # Act
    result = await flow.async_step_sensor(user_input)

    # Assert
    assert result["type"] == FlowResultType.FORM
    assert result["errors"][CONF_ROUTE_ID] == "required"


# ---------------------------------------------------------------------------
# Step 2 — direction_id validation
# ---------------------------------------------------------------------------


async def test_step2_direction_id_2_returns_error(flow: TfiLiveConfigFlow) -> None:
    """Direction_id='2' (out of range) returns invalid_direction error."""
    # Arrange
    flow._config = dict(_PREFILLED_CONFIG, **{CONF_SENSORS: []})
    user_input = {**VALID_STEP2, CONF_DIRECTION_ID: "2"}

    # Act
    result = await flow.async_step_sensor(user_input)

    # Assert
    assert result["type"] == FlowResultType.FORM
    assert result["errors"][CONF_DIRECTION_ID] == "invalid_direction"


async def test_step2_direction_id_non_integer_returns_error(
    flow: TfiLiveConfigFlow,
) -> None:
    """Direction_id='abc' (non-integer) returns invalid_direction error."""
    # Arrange
    flow._config = dict(_PREFILLED_CONFIG, **{CONF_SENSORS: []})
    user_input = {**VALID_STEP2, CONF_DIRECTION_ID: "abc"}

    # Act
    result = await flow.async_step_sensor(user_input)

    # Assert
    assert result["type"] == FlowResultType.FORM
    assert result["errors"][CONF_DIRECTION_ID] == "invalid_direction"


async def test_step2_direction_id_0_accepted(flow: TfiLiveConfigFlow) -> None:
    """Direction_id='0' is a valid value — no direction_id error raised."""
    # Arrange
    flow._config = dict(_PREFILLED_CONFIG, **{CONF_SENSORS: []})
    user_input = {**VALID_STEP2, CONF_DIRECTION_ID: "0"}

    # Act
    result = await flow.async_step_sensor(user_input)

    # Assert — success path shows menu, not a form with errors
    assert result["type"] == FlowResultType.MENU
    assert CONF_DIRECTION_ID not in result.get("errors", {})


async def test_step2_direction_id_1_accepted(flow: TfiLiveConfigFlow) -> None:
    """Direction_id='1' is a valid value — no direction_id error raised."""
    # Arrange
    flow._config = dict(_PREFILLED_CONFIG, **{CONF_SENSORS: []})
    user_input = {**VALID_STEP2, CONF_DIRECTION_ID: "1"}

    # Act
    result = await flow.async_step_sensor(user_input)

    # Assert
    assert result["type"] == FlowResultType.MENU
    assert CONF_DIRECTION_ID not in result.get("errors", {})


# ---------------------------------------------------------------------------
# Step 2 — repeated sensor addition
# ---------------------------------------------------------------------------


async def test_step2_repeated_addition(flow: TfiLiveConfigFlow) -> None:
    """Two sequential valid step 2 submissions produce two sensor entries."""
    # Arrange
    flow._config = {
        CONF_API_KEY: "k",
        CONF_TRIP_UPDATE_URL: "https://a.com",
        CONF_STATIC_GTFS_URL: "https://b.com",
        CONF_SENSORS: [],
    }

    # Act — first sensor
    result1 = await flow.async_step_sensor({**VALID_STEP2, "name": "First"})
    assert result1["type"] == FlowResultType.MENU

    # Act — second sensor (simulates the user returning via async_step_add_another)
    result2 = await flow.async_step_sensor({**VALID_STEP2, "name": "Second"})
    assert result2["type"] == FlowResultType.MENU

    # Assert — both sensors accumulated in staged config
    assert len(flow._config[CONF_SENSORS]) == 2


async def test_step2_repeated_addition_sensor_names_preserved(
    flow: TfiLiveConfigFlow,
) -> None:
    """Each sensor config entry records the name given at submission time."""
    # Arrange
    flow._config = {
        CONF_API_KEY: "k",
        CONF_TRIP_UPDATE_URL: "https://a.com",
        CONF_STATIC_GTFS_URL: "https://b.com",
        CONF_SENSORS: [],
    }

    # Act
    await flow.async_step_sensor({**VALID_STEP2, "name": "Alpha"})
    await flow.async_step_sensor({**VALID_STEP2, "name": "Beta"})

    # Assert
    names = [s["name"] for s in flow._config[CONF_SENSORS]]
    assert names == ["Alpha", "Beta"]


async def test_step2_add_another_loops_back_to_sensor_form(
    flow: TfiLiveConfigFlow,
) -> None:
    """Async_step_add_another presents the sensor form (step_id='sensor')."""
    # Arrange
    flow._config = dict(_PREFILLED_CONFIG, **{CONF_SENSORS: []})

    # Act
    result = await flow.async_step_add_another()

    # Assert — sensor form is re-shown with no errors
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "sensor"
    assert result["errors"] == {}


async def test_step2_finish_creates_entry_with_all_sensors(
    flow: TfiLiveConfigFlow,
) -> None:
    """Async_step_finish creates a config entry whose data contains sensors."""
    # Arrange — pre-populate two sensors
    flow._config = {
        CONF_API_KEY: "k",
        CONF_TRIP_UPDATE_URL: "https://a.com",
        CONF_STATIC_GTFS_URL: "https://b.com",
        CONF_SENSORS: [
            {"name": "S1", CONF_STOP_ID: "A", CONF_ROUTE_ID: "1"},
            {"name": "S2", CONF_STOP_ID: "B", CONF_ROUTE_ID: "2"},
        ],
    }

    # Act
    result = await flow.async_step_finish()

    # Assert
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == "TFI Live"
    assert len(result["data"][CONF_SENSORS]) == 2


async def test_step2_no_network_call(flow: TfiLiveConfigFlow) -> None:
    """Step 2 must not make any HTTP calls during validation."""
    # Arrange
    flow._config = dict(_PREFILLED_CONFIG, **{CONF_SENSORS: []})

    def _raise(*args: object, **kwargs: object) -> None:
        raise AssertionError("HTTP call made during config flow step 2")

    with (
        patch("aiohttp.ClientSession", side_effect=_raise),
        patch("urllib.request.urlopen", side_effect=_raise),
    ):
        result = await flow.async_step_sensor(VALID_STEP2)

    assert result["type"] in (FlowResultType.FORM, FlowResultType.MENU)


# ---------------------------------------------------------------------------
# Re-auth flow
# ---------------------------------------------------------------------------


async def test_reauth_preserves_other_config(
    flow: TfiLiveConfigFlow, mock_hass: MagicMock
) -> None:
    """Re-auth updates only api_key; all other config keys are unchanged."""
    # Arrange
    existing_data = {
        CONF_API_KEY: "old-key",
        CONF_TRIP_UPDATE_URL: "https://original.com",
        CONF_STATIC_GTFS_URL: "https://gtfs.com",
        CONF_SENSORS: [{"name": "test"}],
    }
    mock_entry = MagicMock()
    mock_entry.data = existing_data
    mock_entry.entry_id = "test_entry"
    mock_hass.config_entries.async_get_entry.return_value = mock_entry
    flow.context = {"entry_id": "test_entry"}

    # Act — start the re-auth flow then submit confirmation
    await flow.async_step_reauth({})
    result = await flow.async_step_reauth_confirm({CONF_API_KEY: "new-key"})

    # Assert — flow aborts with reauth_successful
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"

    # Assert — async_update_entry was called with the merged data
    mock_hass.config_entries.async_update_entry.assert_called_once()
    call_kwargs = mock_hass.config_entries.async_update_entry.call_args[1]
    updated_data = call_kwargs["data"]

    assert updated_data[CONF_API_KEY] == "new-key"
    assert updated_data[CONF_TRIP_UPDATE_URL] == "https://original.com"
    assert updated_data[CONF_STATIC_GTFS_URL] == "https://gtfs.com"
    assert updated_data[CONF_SENSORS] == [{"name": "test"}]


async def test_reauth_triggers_reload(
    flow: TfiLiveConfigFlow, mock_hass: MagicMock
) -> None:
    """After successful re-auth, async_reload is called for the entry."""
    # Arrange
    mock_entry = MagicMock()
    mock_entry.data = {
        CONF_API_KEY: "old",
        CONF_TRIP_UPDATE_URL: "https://a.com",
        CONF_STATIC_GTFS_URL: "https://b.com",
        CONF_SENSORS: [],
    }
    mock_entry.entry_id = "eid"
    mock_hass.config_entries.async_get_entry.return_value = mock_entry
    flow.context = {"entry_id": "eid"}

    # Act
    await flow.async_step_reauth({})
    await flow.async_step_reauth_confirm({CONF_API_KEY: "brand-new-key"})

    # Assert — reload was called with the entry_id
    mock_hass.config_entries.async_reload.assert_awaited_once_with("eid")


async def test_reauth_empty_api_key_returns_error(flow: TfiLiveConfigFlow) -> None:
    """Submitting empty api_key returns form error 'required'."""
    # Arrange — _entry must be set as if async_step_reauth has already run
    mock_entry = MagicMock()
    mock_entry.data = {
        CONF_API_KEY: "old",
        CONF_TRIP_UPDATE_URL: "https://a.com",
        CONF_STATIC_GTFS_URL: "https://b.com",
        CONF_SENSORS: [],
    }
    flow._entry = mock_entry

    # Act
    result = await flow.async_step_reauth_confirm({CONF_API_KEY: ""})

    # Assert
    assert result["type"] == FlowResultType.FORM
    assert result["errors"][CONF_API_KEY] == "required"


async def test_reauth_whitespace_api_key_returns_error(
    flow: TfiLiveConfigFlow,
) -> None:
    """Whitespace-only api_key is treated as empty."""
    # Arrange
    mock_entry = MagicMock()
    mock_entry.data = {CONF_API_KEY: "old"}
    flow._entry = mock_entry

    # Act
    result = await flow.async_step_reauth_confirm({CONF_API_KEY: "   "})

    # Assert
    assert result["type"] == FlowResultType.FORM
    assert result["errors"][CONF_API_KEY] == "required"


async def test_reauth_confirm_no_input_shows_form(flow: TfiLiveConfigFlow) -> None:
    """Calling async_step_reauth_confirm(None) renders the form."""
    # Act
    result = await flow.async_step_reauth_confirm(None)

    # Assert
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {}


# ---------------------------------------------------------------------------
# Step 1 — initial render (no user_input)
# ---------------------------------------------------------------------------


async def test_step1_initial_render_returns_form(flow: TfiLiveConfigFlow) -> None:
    """Step 1 called with no input renders the user form with no errors."""
    # Act
    result = await flow.async_step_user(None)

    # Assert
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {}


async def test_step1_initial_render_schema_has_defaults(
    flow: TfiLiveConfigFlow,
) -> None:
    """Step 1 schema includes the expected default URL values."""
    # Act
    result = await flow.async_step_user(None)

    # Assert — schema is present and includes the two URL keys with defaults
    schema = result["data_schema"]
    assert schema is not None
    schema_keys = {str(k): k for k in schema.schema}
    assert CONF_TRIP_UPDATE_URL in schema_keys
    assert CONF_STATIC_GTFS_URL in schema_keys
    # Inspect defaults on the vol.Required keys
    for key in schema.schema:
        if str(key) == CONF_TRIP_UPDATE_URL:
            assert key.default() == DEFAULT_TRIP_UPDATE_URL
        if str(key) == CONF_STATIC_GTFS_URL:
            assert key.default() == DEFAULT_STATIC_GTFS_URL


# ---------------------------------------------------------------------------
# Step 2 — initial render (no user_input)
# ---------------------------------------------------------------------------


async def test_step2_initial_render_returns_form(flow: TfiLiveConfigFlow) -> None:
    """Step 2 called with no input renders the sensor form with no errors."""
    # Arrange
    flow._config = dict(_PREFILLED_CONFIG, **{CONF_SENSORS: []})

    # Act
    result = await flow.async_step_sensor(None)

    # Assert
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "sensor"
    assert result["errors"] == {}


# ---------------------------------------------------------------------------
# Step 2 — successful submission stores correct sensor data
# ---------------------------------------------------------------------------


async def test_step2_valid_submission_stores_sensor_config(
    flow: TfiLiveConfigFlow,
) -> None:
    """A valid step 2 submission appends the correct dict to _config."""
    # Arrange
    flow._config = dict(_PREFILLED_CONFIG, **{CONF_SENSORS: []})
    user_input = {
        "name": "My Bus",
        CONF_STOP_ID: "8220DB002081",
        CONF_ROUTE_ID: "46A",
        CONF_DIRECTION_ID: "1",
        "operator_id": "BE",
    }

    # Act
    result = await flow.async_step_sensor(user_input)

    # Assert — menu offered after success
    assert result["type"] == FlowResultType.MENU
    stored = flow._config[CONF_SENSORS][0]
    assert stored["name"] == "My Bus"
    assert stored[CONF_STOP_ID] == "8220DB002081"
    assert stored[CONF_ROUTE_ID] == "46A"
    assert stored[CONF_DIRECTION_ID] == 1  # stored as int
    assert stored["operator_id"] == "BE"


async def test_step2_empty_direction_id_stored_as_none(
    flow: TfiLiveConfigFlow,
) -> None:
    """Omitting direction_id stores None (not empty string) in the config."""
    # Arrange
    flow._config = dict(_PREFILLED_CONFIG, **{CONF_SENSORS: []})
    user_input = {**VALID_STEP2, CONF_DIRECTION_ID: ""}

    # Act
    await flow.async_step_sensor(user_input)

    # Assert
    assert flow._config[CONF_SENSORS][0][CONF_DIRECTION_ID] is None


async def test_step2_empty_operator_id_stored_as_none(
    flow: TfiLiveConfigFlow,
) -> None:
    """Omitting operator_id stores None (not empty string) in the config."""
    # Arrange
    flow._config = dict(_PREFILLED_CONFIG, **{CONF_SENSORS: []})
    user_input = {**VALID_STEP2, "operator_id": ""}

    # Act
    await flow.async_step_sensor(user_input)

    # Assert
    assert flow._config[CONF_SENSORS][0]["operator_id"] is None


# ---------------------------------------------------------------------------
# Step 1 — both URL errors reported independently
# ---------------------------------------------------------------------------


async def test_step1_both_urls_invalid_reports_both_errors(
    flow: TfiLiveConfigFlow,
) -> None:
    """When both URLs are invalid, both field errors are present."""
    # Arrange
    user_input = {
        CONF_API_KEY: "key",
        CONF_TRIP_UPDATE_URL: "bad",
        CONF_STATIC_GTFS_URL: "also-bad",
    }

    # Act
    result = await flow.async_step_user(user_input)

    # Assert
    assert result["type"] == FlowResultType.FORM
    assert result["errors"][CONF_TRIP_UPDATE_URL] == "invalid_url"
    assert result["errors"][CONF_STATIC_GTFS_URL] == "invalid_url"


# ---------------------------------------------------------------------------
# Issue #28 — duplicate entry guard
# ---------------------------------------------------------------------------


async def test_step1_aborts_when_already_configured(flow: TfiLiveConfigFlow) -> None:
    """Issue #28: starting a second config flow aborts with 'already_configured'.

    Simulates the scenario where TFI Live has already been set up.
    ``_abort_if_unique_id_configured`` raises ``AbortFlow`` (the real HA
    behaviour) when an entry with the same unique_id already exists; this
    propagates out of ``async_step_user`` and is the signal the HA flow runner
    uses to deliver the abort result to the UI.
    """
    # Arrange — stub async_set_unique_id (async, no-op) and make
    # _abort_if_unique_id_configured raise AbortFlow as HA would.

    async def _noop_set_unique_id(unique_id: str) -> None:
        """No-op stub: records that DOMAIN was set as the unique ID."""

    def _raise_abort() -> None:
        raise AbortFlow("already_configured")

    flow.async_set_unique_id = _noop_set_unique_id  # type: ignore[method-assign]
    flow._abort_if_unique_id_configured = _raise_abort  # type: ignore[method-assign]

    # Act — AbortFlow propagates out of async_step_user; pytest.raises captures it.
    with pytest.raises(AbortFlow) as exc_info:
        await flow.async_step_user(None)

    # Assert — the abort reason is "already_configured"
    assert exc_info.value.reason == "already_configured"


# ---------------------------------------------------------------------------
# Issue #34 — reconfigure flow
# ---------------------------------------------------------------------------


def _make_flow_with_reconfigure_entry(
    mock_hass: MagicMock,
) -> tuple[TfiLiveConfigFlow, MagicMock]:
    """Return a configured flow and mock entry for reconfigure tests.

    Args:
        mock_hass: A mock hass instance from the ``mock_hass`` fixture.

    Returns:
        A tuple of (flow, mock_entry) where the flow has been wired up with
        base-class stubs and a reconfigure entry.
    """
    existing_data = {
        CONF_API_KEY: "old-key",
        CONF_TRIP_UPDATE_URL: "https://old.example.com",
        CONF_STATIC_GTFS_URL: "https://gtfs.example.com",
        CONF_SENSORS: [{"name": "Existing sensor"}],
    }
    mock_entry = MagicMock()
    mock_entry.data = existing_data
    mock_entry.entry_id = "reconfigure_entry"

    f = TfiLiveConfigFlow()
    f.hass = mock_hass
    f.context = {"entry_id": "reconfigure_entry", "source": "reconfigure"}

    f.async_show_form = lambda **kwargs: {
        "type": FlowResultType.FORM,
        "step_id": kwargs.get("step_id"),
        "errors": kwargs.get("errors", {}),
        "data_schema": kwargs.get("data_schema"),
    }
    f.async_update_reload_and_abort = lambda entry, data, reason: {
        "type": FlowResultType.ABORT,
        "reason": reason,
        "_updated_data": data,
    }

    f._get_reconfigure_entry = lambda: mock_entry  # type: ignore[method-assign]

    return f, mock_entry


async def test_reconfigure_happy_path(mock_hass: MagicMock) -> None:
    """Issue #34: reconfigure updates credentials and preserves sensor list."""
    flow, mock_entry = _make_flow_with_reconfigure_entry(mock_hass)

    new_input = {
        CONF_API_KEY: "new-key",
        CONF_TRIP_UPDATE_URL: "https://new.example.com",
        CONF_STATIC_GTFS_URL: "https://newgtfs.example.com",
    }
    mock_session = _make_mock_session(status=200)
    with patch(
        "custom_components.ha_tfi_live.config_flow.async_get_clientsession",
        return_value=mock_session,
    ):
        result = await flow.async_step_reconfigure(new_input)

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    updated = result["_updated_data"]
    assert updated[CONF_API_KEY] == "new-key"
    assert updated[CONF_TRIP_UPDATE_URL] == "https://new.example.com"
    assert updated[CONF_STATIC_GTFS_URL] == "https://newgtfs.example.com"
    # Sensor list from existing entry is preserved
    assert updated[CONF_SENSORS] == [{"name": "Existing sensor"}]


async def test_reconfigure_invalid_auth(mock_hass: MagicMock) -> None:
    """Issue #34: reconfigure with 401 re-shows form with invalid_auth error."""
    flow, _ = _make_flow_with_reconfigure_entry(mock_hass)

    new_input = {
        CONF_API_KEY: "bad-key",
        CONF_TRIP_UPDATE_URL: "https://new.example.com",
        CONF_STATIC_GTFS_URL: "https://newgtfs.example.com",
    }
    mock_session = _make_mock_session(status=401)
    with patch(
        "custom_components.ha_tfi_live.config_flow.async_get_clientsession",
        return_value=mock_session,
    ):
        result = await flow.async_step_reconfigure(new_input)

    assert result["type"] == FlowResultType.FORM
    assert result["errors"].get("base") == "invalid_auth"


async def test_reconfigure_initial_render_prefills_entry_data(
    mock_hass: MagicMock,
) -> None:
    """Issue #34: reconfigure initial render shows form prefilled with entry data."""
    flow, mock_entry = _make_flow_with_reconfigure_entry(mock_hass)

    result = await flow.async_step_reconfigure(None)

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    assert result["errors"] == {}
    # Verify the schema defaults match the existing entry
    schema = result["data_schema"]
    assert schema is not None
    for key in schema.schema:
        if str(key) == CONF_API_KEY:
            assert key.default() == mock_entry.data[CONF_API_KEY]
        if str(key) == CONF_TRIP_UPDATE_URL:
            assert key.default() == mock_entry.data[CONF_TRIP_UPDATE_URL]


# ---------------------------------------------------------------------------
# Issue #34 — options flow
# ---------------------------------------------------------------------------


@pytest.fixture
def options_flow(mock_hass: MagicMock) -> TfiLiveOptionsFlowHandler:
    """Return a TfiLiveOptionsFlowHandler wired up with stub base-class methods.

    Args:
        mock_hass: A mock hass instance.

    Returns:
        A :class:`TfiLiveOptionsFlowHandler` with stubbed HA base-class methods
        and a mock ``config_entry`` containing an empty sensor list.
    """
    existing_data = {
        CONF_API_KEY: "k",
        CONF_TRIP_UPDATE_URL: "https://a.com",
        CONF_STATIC_GTFS_URL: "https://b.com",
        CONF_SENSORS: [],
    }
    mock_entry = MagicMock()
    mock_entry.data = existing_data
    mock_entry.domain = "ha_tfi_live"

    # config_entry is a read-only property on OptionsFlow that calls
    # hass.config_entries.async_get_known_entry(handler).  Wire up the mock
    # so that call returns our mock entry.
    mock_hass.config_entries.async_get_known_entry = MagicMock(return_value=mock_entry)

    handler = TfiLiveOptionsFlowHandler()
    handler.hass = mock_hass
    # handler is the "entry_id" used by the property
    handler.handler = mock_entry.entry_id  # type: ignore[assignment]

    handler.async_show_form = lambda **kwargs: {  # type: ignore[method-assign]
        "type": FlowResultType.FORM,
        "step_id": kwargs.get("step_id"),
        "errors": kwargs.get("errors", {}),
        "data_schema": kwargs.get("data_schema"),
    }
    handler.async_show_menu = lambda **kwargs: {  # type: ignore[method-assign]
        "type": FlowResultType.MENU,
        "step_id": kwargs.get("step_id"),
        "menu_options": kwargs.get("menu_options", []),
    }
    handler.async_create_entry = lambda title, data: {  # type: ignore[method-assign]
        "type": FlowResultType.CREATE_ENTRY,
        "title": title,
        "data": data,
    }

    return handler


async def test_options_flow_init_shows_sensor_form(
    options_flow: TfiLiveOptionsFlowHandler,
) -> None:
    """Issue #34: options flow init delegates to the sensor form."""
    result = await options_flow.async_step_init()

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "sensor"


async def test_options_flow_adding_sensor_appends_to_entry_data(
    options_flow: TfiLiveOptionsFlowHandler,
    mock_hass: MagicMock,
) -> None:
    """Issue #34: adding a sensor via options flow appends it to entry data."""
    # Pre-populate an existing sensor in the mock entry returned by config_entry
    existing_data_with_sensor = {
        CONF_API_KEY: "k",
        CONF_TRIP_UPDATE_URL: "https://a.com",
        CONF_STATIC_GTFS_URL: "https://b.com",
        CONF_SENSORS: [{"name": "Existing", CONF_STOP_ID: "S1", CONF_ROUTE_ID: "R1"}],
    }
    mock_hass.config_entries.async_get_known_entry.return_value.data = (
        existing_data_with_sensor
    )

    # Submit a valid new sensor
    await options_flow.async_step_sensor({**VALID_STEP2, "name": "New Sensor"})

    # Finish the options flow
    result = await options_flow.async_step_finish()

    assert result["type"] == FlowResultType.CREATE_ENTRY
    sensors = result["data"][CONF_SENSORS]
    assert len(sensors) == 2
    assert sensors[0]["name"] == "Existing"
    assert sensors[1]["name"] == "New Sensor"


async def test_options_flow_sensor_validation_errors(
    options_flow: TfiLiveOptionsFlowHandler,
) -> None:
    """Issue #34: options flow sensor form validates required fields."""
    result = await options_flow.async_step_sensor(
        {
            "name": "",
            CONF_STOP_ID: "",
            CONF_ROUTE_ID: "",
            CONF_DIRECTION_ID: "",
            "operator_id": "",
        }
    )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"].get("name") == "required"
    assert result["errors"].get(CONF_STOP_ID) == "required"
    assert result["errors"].get(CONF_ROUTE_ID) == "required"


async def test_options_flow_add_another_loops(
    options_flow: TfiLiveOptionsFlowHandler,
) -> None:
    """Issue #34: options flow add_another returns to the sensor form."""
    result = await options_flow.async_step_add_another()

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "sensor"


async def test_options_flow_direction_id_invalid(
    options_flow: TfiLiveOptionsFlowHandler,
) -> None:
    """Issue #34: options flow direction_id='2' returns invalid_direction error."""
    result = await options_flow.async_step_sensor(
        {
            "name": "Bus",
            CONF_STOP_ID: "S1",
            CONF_ROUTE_ID: "46A",
            CONF_DIRECTION_ID: "2",
            "operator_id": "",
        }
    )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"].get(CONF_DIRECTION_ID) == "invalid_direction"


async def test_options_flow_direction_id_non_integer(
    options_flow: TfiLiveOptionsFlowHandler,
) -> None:
    """Issue #34: options flow direction_id='abc' returns invalid_direction error."""
    result = await options_flow.async_step_sensor(
        {
            "name": "Bus",
            CONF_STOP_ID: "S1",
            CONF_ROUTE_ID: "46A",
            CONF_DIRECTION_ID: "abc",
            "operator_id": "",
        }
    )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"].get(CONF_DIRECTION_ID) == "invalid_direction"


async def test_reconfigure_empty_api_key_returns_error(mock_hass: MagicMock) -> None:
    """Issue #34: reconfigure with empty api_key re-shows form with required error."""
    flow, _ = _make_flow_with_reconfigure_entry(mock_hass)

    result = await flow.async_step_reconfigure(
        {
            CONF_API_KEY: "",
            CONF_TRIP_UPDATE_URL: "https://new.example.com",
            CONF_STATIC_GTFS_URL: "https://newgtfs.example.com",
        }
    )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"].get(CONF_API_KEY) == "required"


async def test_reconfigure_invalid_trip_url_returns_error(mock_hass: MagicMock) -> None:
    """Issue #34: reconfigure with bad trip_update_url returns invalid_url error."""
    flow, _ = _make_flow_with_reconfigure_entry(mock_hass)

    result = await flow.async_step_reconfigure(
        {
            CONF_API_KEY: "new-key",
            CONF_TRIP_UPDATE_URL: "not-a-url",
            CONF_STATIC_GTFS_URL: "https://newgtfs.example.com",
        }
    )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"].get(CONF_TRIP_UPDATE_URL) == "invalid_url"


async def test_reconfigure_invalid_static_url_returns_error(
    mock_hass: MagicMock,
) -> None:
    """Issue #34: reconfigure with bad static_gtfs_url returns invalid_url error."""
    flow, _ = _make_flow_with_reconfigure_entry(mock_hass)

    result = await flow.async_step_reconfigure(
        {
            CONF_API_KEY: "new-key",
            CONF_TRIP_UPDATE_URL: "https://new.example.com",
            CONF_STATIC_GTFS_URL: "not-a-url",
        }
    )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"].get(CONF_STATIC_GTFS_URL) == "invalid_url"


def test_async_get_options_flow_returns_handler() -> None:
    """Issue #34: async_get_options_flow returns a TfiLiveOptionsFlowHandler."""
    mock_entry = MagicMock()
    handler = TfiLiveConfigFlow.async_get_options_flow(mock_entry)

    assert isinstance(handler, TfiLiveOptionsFlowHandler)
