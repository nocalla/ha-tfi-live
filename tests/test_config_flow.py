"""Tests for custom_components.tfi_live.config_flow.

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

from collections.abc import Callable
from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.data_entry_flow import AbortFlow, FlowResultType
from nta_gtfs import GtfsRtAuthError, GtfsRtFetchError, GtfsRtParseError

from custom_components.tfi_live.config_flow import (
    TfiLiveConfigFlow,
    TfiLiveOptionsFlowHandler,
)
from custom_components.tfi_live.const import (
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
    CONF_TRIP_UPDATE_URL: ("https://api.nationaltransport.ie/gtfsr/v2/TripUpdates"),
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
# Stub factories
# ---------------------------------------------------------------------------


def _assert_step_exists(flow: object, step_id: str | None) -> None:
    """Mirror HA's ``_raise_if_step_does_not_exist`` step validation.

    Home Assistant's flow manager raises ``UnknownStep`` (surfaced to the
    user as "Invalid flow specified", issue #78) when a FORM or MENU result
    carries a ``step_id`` — or a menu option — with no matching
    ``async_step_<step_id>`` method on the handler. The stubs replicate
    that check so tests fail the same way production HA does.

    Args:
        flow: The flow handler under test.
        step_id: The step id referenced by the flow result.

    Raises:
        AssertionError: If the handler lacks the matching step method.
    """
    assert step_id is not None, "flow result is missing a step_id"
    method = f"async_step_{step_id}"
    assert hasattr(flow, method), (
        f"Handler {type(flow).__name__} doesn't support step {step_id} "
        f"(missing {method}); real HA would raise UnknownStep"
    )


def _stub_show_form(flow: object) -> Callable[..., dict[str, Any]]:
    """Return an ``async_show_form`` stub that validates the step exists.

    Args:
        flow: The flow handler the stub will be attached to.

    Returns:
        A callable matching the ``async_show_form`` signature that returns
        a FORM result dict after validating the ``step_id``.
    """

    def _show_form(**kwargs: Any) -> dict[str, Any]:
        _assert_step_exists(flow, kwargs.get("step_id"))
        return {
            "type": FlowResultType.FORM,
            "step_id": kwargs.get("step_id"),
            "errors": kwargs.get("errors", {}),
            "data_schema": kwargs.get("data_schema"),
        }

    return _show_form


def _stub_show_menu(flow: object) -> Callable[..., dict[str, Any]]:
    """Return an ``async_show_menu`` stub that validates all steps exist.

    Validates both the menu's own ``step_id`` and every menu option,
    since HA routes each selected option to ``async_step_<option>``.

    Args:
        flow: The flow handler the stub will be attached to.

    Returns:
        A callable matching the ``async_show_menu`` signature that returns
        a MENU result dict after validating step ids.
    """

    def _show_menu(**kwargs: Any) -> dict[str, Any]:
        _assert_step_exists(flow, kwargs.get("step_id"))
        for option in kwargs.get("menu_options", []):
            _assert_step_exists(flow, option)
        return {
            "type": FlowResultType.MENU,
            "step_id": kwargs.get("step_id"),
            "menu_options": kwargs.get("menu_options", []),
        }

    return _show_menu


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

    f.async_show_form = _stub_show_form(f)
    f.async_show_menu = _stub_show_menu(f)
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


@contextmanager
def _patch_probe_client(side_effect: Exception | None = None):  # type: ignore[no-untyped-def]
    """Patch the config flow's GtfsRtClient class with a probe mock.

    Follows the project convention of mocking ``nta_gtfs.GtfsRtClient`` at
    the class level rather than mocking raw aiohttp sessions.  The HTTP
    session getter is also stubbed so no real ClientSession is created for
    the mock hass instance.

    Args:
        side_effect: Exception raised by ``async_fetch_trip_updates``, or
            ``None`` for a successful probe returning an empty feed.

    Yields:
        The mock client instance the flow's probe will use.
    """
    client = MagicMock()
    client.async_fetch_trip_updates = AsyncMock(
        return_value=[], side_effect=side_effect
    )
    with (
        patch(
            "custom_components.tfi_live.config_flow.async_get_clientsession",
            return_value=MagicMock(),
        ),
        patch(
            "custom_components.tfi_live.config_flow.GtfsRtClient",
            MagicMock(return_value=client),
        ),
    ):
        yield client


async def test_step1_valid_advances_to_sensor(flow: TfiLiveConfigFlow) -> None:
    """Valid step 1 input advances to the sensor form (step_id='sensor')."""
    # Arrange — mock the probe so no real HTTP call is made
    with _patch_probe_client():
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
    """Issue #27: step 1 probe auth failure re-shows form with invalid_auth error."""
    # Arrange
    with _patch_probe_client(side_effect=GtfsRtAuthError("HTTP 401")):
        result = await flow.async_step_user(VALID_STEP1)

    # Assert
    assert result["type"] == FlowResultType.FORM
    assert result["errors"].get("base") == "invalid_auth"


async def test_step1_cannot_connect(flow: TfiLiveConfigFlow) -> None:
    """Issue #27: step 1 probe fetch failure re-shows form with cannot_connect."""
    # Arrange — simulate a connection error from the probe
    with _patch_probe_client(side_effect=GtfsRtFetchError("connection refused")):
        result = await flow.async_step_user(VALID_STEP1)

    # Assert
    assert result["type"] == FlowResultType.FORM
    assert result["errors"].get("base") == "cannot_connect"


async def test_step1_http_500_returns_cannot_connect(flow: TfiLiveConfigFlow) -> None:
    """Issue #27: step 1 probe HTTP 5xx re-shows form with cannot_connect."""
    with _patch_probe_client(side_effect=GtfsRtFetchError("HTTP 500")):
        result = await flow.async_step_user(VALID_STEP1)

    assert result["type"] == FlowResultType.FORM
    assert result["errors"].get("base") == "cannot_connect"


async def test_step1_unparseable_feed_returns_cannot_parse(
    flow: TfiLiveConfigFlow,
) -> None:
    """Issue #99: a feed the client cannot parse fails the wizard with cannot_parse.

    The v0.2.4 probe only checked the HTTP status code, so a URL carrying
    ``format=json`` (HTTP 200, JSON body) passed validation and then put the
    entry into a permanent setup-retry loop. The probe now parses the feed
    via GtfsRtClient, so a GtfsRtParseError must surface as a form error.
    """
    with _patch_probe_client(
        side_effect=GtfsRtParseError("Invalid protobuf FeedMessage")
    ):
        result = await flow.async_step_user(VALID_STEP1)

    assert result["type"] == FlowResultType.FORM
    assert result["errors"].get("base") == "cannot_parse"


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


# ---------------------------------------------------------------------------
# Issue #78 — sensor_menu must be a real step on the handler
# ---------------------------------------------------------------------------


async def test_step2_success_menu_step_is_real(flow: TfiLiveConfigFlow) -> None:
    """Issue #78: valid sensor submission shows a menu backed by a real step.

    The v0.2.3 regression returned a menu with step_id='sensor_menu' but no
    async_step_sensor_menu method, so HA raised UnknownStep and the frontend
    showed "Invalid flow specified". The validating async_show_menu stub
    fails this test if the step method is ever removed again.
    """
    # Arrange
    flow._config = dict(_PREFILLED_CONFIG, **{CONF_SENSORS: []})

    # Act
    result = await flow.async_step_sensor(dict(VALID_STEP2))

    # Assert
    assert result["type"] == FlowResultType.MENU
    assert result["step_id"] == "sensor_menu"
    assert result["menu_options"] == ["add_another", "finish"]


async def test_sensor_menu_step_renders_menu(flow: TfiLiveConfigFlow) -> None:
    """Issue #78: async_step_sensor_menu itself renders the post-add menu."""
    # Act
    result = await flow.async_step_sensor_menu()

    # Assert
    assert result["type"] == FlowResultType.MENU
    assert result["step_id"] == "sensor_menu"
    assert result["menu_options"] == ["add_another", "finish"]


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
# Issue #99 — default trip update URL must be the protobuf endpoint
# ---------------------------------------------------------------------------


def test_default_trip_update_url_has_no_format_param() -> None:
    """Issue #99: the default trip URL must not request the JSON rendering.

    The NTA endpoint returns protobuf by default; a ``format=json`` query
    parameter yields JSON, which nta_gtfs.GtfsRtClient cannot parse.
    """
    assert "format=json" not in DEFAULT_TRIP_UPDATE_URL
    assert "?" not in DEFAULT_TRIP_UPDATE_URL


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

    f.async_show_form = _stub_show_form(f)
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
    with _patch_probe_client():
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
    with _patch_probe_client(side_effect=GtfsRtAuthError("HTTP 401")):
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
    mock_entry.domain = "tfi_live"

    # config_entry is a read-only property on OptionsFlow that calls
    # hass.config_entries.async_get_known_entry(handler).  Wire up the mock
    # so that call returns our mock entry.
    mock_hass.config_entries.async_get_known_entry = MagicMock(return_value=mock_entry)

    handler = TfiLiveOptionsFlowHandler()
    handler.hass = mock_hass
    # handler is the "entry_id" used by the property
    handler.handler = mock_entry.entry_id  # type: ignore[assignment]

    handler.async_show_form = _stub_show_form(handler)  # type: ignore[method-assign]
    handler.async_show_menu = _stub_show_menu(handler)  # type: ignore[method-assign]
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


async def test_options_flow_success_menu_step_is_real(
    options_flow: TfiLiveOptionsFlowHandler,
) -> None:
    """Issue #78: options flow sensor success shows a menu backed by a real step."""
    result = await options_flow.async_step_sensor(dict(VALID_STEP2))

    assert result["type"] == FlowResultType.MENU
    assert result["step_id"] == "sensor_menu"
    assert result["menu_options"] == ["add_another", "finish"]


async def test_options_flow_sensor_menu_step_renders_menu(
    options_flow: TfiLiveOptionsFlowHandler,
) -> None:
    """Issue #78: options flow async_step_sensor_menu renders the post-add menu."""
    result = await options_flow.async_step_sensor_menu()

    assert result["type"] == FlowResultType.MENU
    assert result["step_id"] == "sensor_menu"
    assert result["menu_options"] == ["add_another", "finish"]


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
