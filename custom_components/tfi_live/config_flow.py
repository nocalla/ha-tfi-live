"""Config flow for the TFI Live integration.

Handles the two-step initial setup (step 1: integration-level settings;
step 2: add one or more sensors via a searchable stop/route picker), the
re-authentication flow triggered when the GTFS-RT feed returns HTTP 401,
the reconfiguration flow for updating credentials, and the options flow
for adding sensors after setup.
"""

from __future__ import annotations

import asyncio
import logging
import urllib.parse
from collections.abc import Callable
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry, ConfigFlowResult
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)
from nta_gtfs import (
    GtfsRtAuthError,
    GtfsRtClient,
    GtfsRtParseError,
    Route,
    StaticGtfsLoadError,
    StaticGtfsPickerClient,
    Stop,
)

from .const import (
    ALL_ROUTES_SENTINEL,
    CONF_API_KEY,
    CONF_DIRECTION_ID,
    CONF_NUM_DEPARTURES,
    CONF_OPERATOR_ID,
    CONF_ROUTE_ID,
    CONF_SENSORS,
    CONF_STATIC_GTFS_URL,
    CONF_STOP_ID,
    CONF_TRIP_UPDATE_URL,
    DEFAULT_NUM_DEPARTURES,
    DEFAULT_STATIC_GTFS_URL,
    DEFAULT_TRIP_UPDATE_URL,
    DOMAIN,
    NUM_DEPARTURES_MAX,
    NUM_DEPARTURES_MIN,
)

_LOGGER = logging.getLogger(__name__)

_SENSOR_NAME_KEY = "name"

# Sentinel used for the post-sensor-add menu choice.
_MENU_ADD_ANOTHER = "add_another"
_MENU_FINISH = "finish"

_ALL_ROUTES_LABEL = "All routes at this stop"


def _url_validator(value: str) -> str:
    """Validate that a string is a parseable URL with scheme and host.

    Args:
        value: The string to validate.

    Returns:
        The original value unchanged if valid.

    Raises:
        vol.Invalid: If the value cannot be parsed as a URL with both a
            scheme and a non-empty host.
    """
    try:
        parsed = urllib.parse.urlparse(value)
        if not parsed.scheme or not parsed.netloc:
            raise vol.Invalid("invalid_url")
    except Exception as exc:
        raise vol.Invalid("invalid_url") from exc
    return value


def _validate_num_departures(value: str) -> int:
    """Validate and parse the "number of upcoming services" field.

    Args:
        value: The raw string entered by the user.

    Returns:
        The parsed integer value.

    Raises:
        vol.Invalid: If the value is not an integer between
            ``NUM_DEPARTURES_MIN`` and ``NUM_DEPARTURES_MAX`` inclusive.
    """
    try:
        parsed = int(value)
    except ValueError as exc:
        raise vol.Invalid("invalid_num_departures") from exc
    if not (NUM_DEPARTURES_MIN <= parsed <= NUM_DEPARTURES_MAX):
        raise vol.Invalid("invalid_num_departures")
    return parsed


async def _probe_feed(
    hass: Any,
    trip_update_url: str,
    api_key: str,
    errors: dict[str, str],
) -> None:
    """Probe the GTFS-RT feed URL to validate the API key and feed format.

    Fetches and parses the trip update feed through
    :class:`nta_gtfs.GtfsRtClient` — the same client the coordinator uses —
    so a URL that returns a non-protobuf body (e.g. one carrying
    ``format=json``, issue #99) is rejected in the wizard rather than
    failing on every refresh after setup. Populates ``errors["base"]``
    if the probe fails.

    Args:
        hass: The Home Assistant instance used to obtain the HTTP client session.
        trip_update_url: The GTFS-RT trip update feed URL to probe.
        api_key: The NTA API key to include in the request header.
        errors: Mutable dict of form errors; ``"base"`` will be set on failure.
    """
    client = GtfsRtClient(
        feed_url=trip_update_url,
        api_key=api_key,
        session=async_get_clientsession(hass),
    )
    try:
        async with asyncio.timeout(10):
            await client.async_fetch_trip_updates()
    except GtfsRtAuthError:
        errors["base"] = "invalid_auth"
    except GtfsRtParseError as exc:
        _LOGGER.warning(
            "GTFS-RT feed probe of %s returned an unparseable feed: %s",
            trip_update_url,
            exc,
        )
        errors["base"] = "cannot_parse"
    except Exception as exc:
        _LOGGER.warning("GTFS-RT feed probe of %s failed: %s", trip_update_url, exc)
        errors["base"] = "cannot_connect"


def _build_stop_options(stops: list[Stop]) -> list[SelectOptionDict]:
    """Build sorted select options for the stop-picker step.

    Args:
        stops: Stops parsed from the static GTFS feed's ``stops.txt``.

    Returns:
        Select options keyed by real ``stop_id``, labelled with the
        rider-facing ``stop_code`` (falling back to ``stop_id`` when
        ``stop_code`` is blank) and ``stop_name``, sorted by label.
    """
    options = [
        SelectOptionDict(
            value=stop.stop_id,
            label=f"{stop.stop_code or stop.stop_id} — {stop.stop_name}",
        )
        for stop in stops
    ]
    options.sort(key=lambda opt: opt["label"])
    return options


def _build_route_options(
    routes: list[Route], *, show_agency: bool
) -> list[SelectOptionDict]:
    """Build sorted select options for the route-picker step.

    Args:
        routes: Routes to offer — either narrowed to a specific stop or the
            full nationwide list.
        show_agency: When ``True`` (the unnarrowed fallback list), the
            agency ID is appended to each label to help distinguish
            same-numbered routes from different operators, since the
            structural stop-narrowing that normally resolves this
            collision isn't available in the fallback list.

    Returns:
        Select options keyed by real ``route_id``, with a pinned "All
        routes at this stop" entry first, followed by the routes sorted
        by label.
    """
    route_options = [
        SelectOptionDict(
            value=route.route_id,
            label=(
                f"{route.route_short_name} ({route.agency_id or 'unknown agency'})"
                if show_agency
                else route.route_short_name
            ),
        )
        for route in routes
    ]
    route_options.sort(key=lambda opt: opt["label"])
    return [
        SelectOptionDict(value=ALL_ROUTES_SENTINEL, label=_ALL_ROUTES_LABEL),
        *route_options,
    ]


def _direction_label(direction_id: int, termini: list[str]) -> str:
    """Build the direction dropdown label for one direction_id.

    Args:
        direction_id: The GTFS direction (``0`` or ``1``) the label is for.
        termini: Distinct terminus stop names for this direction, already
            sorted; empty when the lookup failed or found nothing.

    Returns:
        ``"towards {termini}"``, joining multiple termini with ``" / "``
        and truncating to the first 3 with a ``" +N more"`` suffix beyond
        that, when ``termini`` is non-empty. Otherwise a plain
        ``"Direction {direction_id}"`` fallback label.
    """
    if not termini:
        return f"Direction {direction_id}"
    shown = " / ".join(termini[:3])
    suffix = f" +{len(termini) - 3} more" if len(termini) > 3 else ""
    return f"towards {shown}{suffix}"


async def _build_direction_options(
    client: StaticGtfsPickerClient | None, stop_id: str
) -> list[SelectOptionDict]:
    """Build select options for the direction-picker field.

    Termini are always resolved across every route serving ``stop_id``
    (``route_id=None``) rather than the specific route the user is about to
    pick, since both fields are submitted together on the same form and the
    route choice isn't known yet when this is rendered. This naturally
    collapses to a single route's termini when only one route serves the
    stop, and merges/deduplicates across routes otherwise. A known
    trade-off: at a stop served by several routes, picking one specific
    route (rather than "All routes at this stop") still shows the label
    merged across every route at the stop, not narrowed to the picked one —
    unavoidable without splitting route and direction into separate steps,
    which is out of scope here.

    Args:
        client: The loaded picker client to query termini with, or ``None``
            when the static GTFS feed failed to load — both real directions
            then degrade to plain numbered labels.
        stop_id: The picked stop to resolve termini for.

    Returns:
        Exactly three select options: "Any direction" (value ``""``,
        pinned first), then direction 0 and direction 1 (values ``"0"``
        and ``"1"``), each labelled with real terminus names when
        available or a plain numbered fallback otherwise.
    """
    labels = {0: "Direction 0", 1: "Direction 1"}
    if client is not None:
        for direction_id in (0, 1):
            try:
                termini = await client.async_get_termini(stop_id, None, direction_id)
            except StaticGtfsLoadError as exc:
                _LOGGER.warning(
                    "Static GTFS termini lookup failed for direction %s: %s",
                    direction_id,
                    exc,
                )
                termini = []
            labels[direction_id] = _direction_label(direction_id, termini)
    return [
        SelectOptionDict(value="", label="Any direction"),
        SelectOptionDict(value="0", label=labels[0]),
        SelectOptionDict(value="1", label=labels[1]),
    ]


class _SensorPickerFlow:
    """Shared stop/route picker steps used by both the config and options flows.

    Holds the session-scoped :class:`StaticGtfsPickerClient` used across
    both picker steps and every sensor added within one flow run (including
    repeated "add another" loops), and the two step implementations
    themselves. Concrete flow handlers must initialise
    ``self._picker_client`` and ``self._pending_stop_id`` to ``None`` and
    implement :meth:`_static_gtfs_url` and :meth:`_append_sensor`.
    """

    hass: HomeAssistant
    _picker_client: StaticGtfsPickerClient | None
    _pending_stop_id: str | None
    # Provided by the concrete FlowHandler subclass (ConfigFlow/OptionsFlow);
    # annotated here (not implemented) so this mixin doesn't shadow them.
    async_show_form: Callable[..., ConfigFlowResult]
    async_show_menu: Callable[..., ConfigFlowResult]

    def _static_gtfs_url(self) -> str:
        """Return the static GTFS feed URL to build the picker client from.

        Returns:
            The static GTFS feed URL for the flow currently in progress.
        """
        raise NotImplementedError

    def _append_sensor(self, sensor_config: dict[str, Any]) -> None:
        """Store a completed sensor config on the concrete flow.

        Args:
            sensor_config: The sensor configuration to stage for saving.
        """
        raise NotImplementedError

    async def _get_picker_client(self) -> StaticGtfsPickerClient:
        """Return the session-scoped picker client, loading it on first use.

        Constructs and loads exactly one ``StaticGtfsPickerClient`` per flow
        run, reused across both picker steps and every sensor added in that
        run. Left unset on load failure so the next attempt retries rather
        than reusing a half-loaded client.

        Returns:
            The loaded, session-scoped picker client.

        Raises:
            StaticGtfsLoadError: If the static GTFS archive can't be
                downloaded or parsed.
        """
        if self._picker_client is None:
            client = StaticGtfsPickerClient(
                self._static_gtfs_url(), async_get_clientsession(self.hass)
            )
            await client.async_load()
            self._picker_client = client
        return self._picker_client

    async def async_step_stop(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the stop-search step of the sensor picker.

        Presents a searchable dropdown of every stop in the static GTFS
        feed, keyed by real ``stop_id`` but labelled with the rider-facing
        stop code and name. On submission, stores the picked stop and
        advances to :meth:`async_step_route`.

        Args:
            user_input: Form data submitted by the user, or ``None`` when
                the form is first rendered.

        Returns:
            A ConfigFlowResult that either re-shows the form with a load
            error, or advances to :meth:`async_step_route`.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            self._pending_stop_id = user_input[CONF_STOP_ID]
            return await self.async_step_route()

        stops: list[Stop] = []
        try:
            client = await self._get_picker_client()
            stops = client.list_stops()
        except StaticGtfsLoadError as exc:
            _LOGGER.warning("Static GTFS picker load failed: %s", exc)
            errors["base"] = "cannot_load_static_gtfs"

        schema = vol.Schema(
            {
                vol.Required(CONF_STOP_ID): SelectSelector(
                    SelectSelectorConfig(
                        options=_build_stop_options(stops),
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="stop",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_route(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the route-narrowing step of the sensor picker.

        Presents a dropdown narrowed to routes with a real ``stop_times.txt``
        link to the picked stop, with a pinned "All routes at this stop"
        entry that leaves ``route_id`` unset for stop-wide monitoring. Falls
        back to the full nationwide route list (agency-labelled) when the
        narrowed list is empty. Also collects the sensor's name, direction,
        and operator. On submission, stages the sensor and advances to
        :meth:`async_step_sensor_menu`.

        Args:
            user_input: Form data submitted by the user, or ``None`` when
                the form is first rendered.

        Returns:
            A ConfigFlowResult that re-shows the form with errors, or
            advances to :meth:`async_step_sensor_menu`.
        """
        errors: dict[str, str] = {}
        stop_id = self._pending_stop_id
        if stop_id is None:
            # Only reachable if async_step_route is entered directly without
            # going through async_step_stop first.
            raise ValueError("async_step_route reached with no pending stop_id")

        if user_input is not None:
            name: str = user_input.get(_SENSOR_NAME_KEY, "").strip()
            route_id_raw: str = user_input[CONF_ROUTE_ID]
            direction_id_raw: str = user_input.get(CONF_DIRECTION_ID, "")
            operator_id: str = user_input.get(CONF_OPERATOR_ID, "").strip()

            if not name:
                errors[_SENSOR_NAME_KEY] = "required"

            direction_id: int | None = (
                int(direction_id_raw) if direction_id_raw else None
            )

            if not errors:
                route_id: str | None = (
                    None if route_id_raw == ALL_ROUTES_SENTINEL else route_id_raw
                )
                self._append_sensor(
                    {
                        _SENSOR_NAME_KEY: name,
                        CONF_STOP_ID: stop_id,
                        CONF_ROUTE_ID: route_id,
                        CONF_DIRECTION_ID: direction_id,
                        CONF_OPERATOR_ID: operator_id or None,
                    }
                )
                self._pending_stop_id = None
                return await self.async_step_sensor_menu()

        routes: list[Route] = []
        show_agency = False
        picker_client: StaticGtfsPickerClient | None = None
        try:
            picker_client = await self._get_picker_client()
            routes = await picker_client.async_get_routes_for_stop(stop_id)
            if not routes:
                routes = picker_client.list_routes()
                show_agency = True
                errors["base"] = "route_list_not_narrowed"
        except StaticGtfsLoadError as exc:
            _LOGGER.warning("Static GTFS picker load failed: %s", exc)
            errors["base"] = "cannot_load_static_gtfs"
            picker_client = None

        direction_options = await _build_direction_options(picker_client, stop_id)

        schema = vol.Schema(
            {
                vol.Required(CONF_ROUTE_ID): SelectSelector(
                    SelectSelectorConfig(
                        options=_build_route_options(routes, show_agency=show_agency),
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Required(_SENSOR_NAME_KEY): str,
                vol.Optional(CONF_DIRECTION_ID, default=""): SelectSelector(
                    SelectSelectorConfig(
                        options=direction_options,
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional(CONF_OPERATOR_ID, default=""): str,
            }
        )
        return self.async_show_form(
            step_id="route",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_sensor_menu(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the post-add menu offering to add another sensor or finish.

        HA requires the ``step_id`` of a menu result to name a real step
        method on the handler, so the menu must live in its own step
        rather than being returned inline from :meth:`async_step_route`.

        Args:
            user_input: Unused; present to satisfy the HA flow handler
                protocol.

        Returns:
            A ConfigFlowResult showing the add-another/finish menu.
        """
        return self.async_show_menu(
            step_id="sensor_menu",
            menu_options=[_MENU_ADD_ANOTHER, _MENU_FINISH],
        )

    async def async_step_add_another(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the 'Add another sensor' menu choice.

        Loops back to :meth:`async_step_stop` so the user can configure
        an additional sensor, reusing the same session-scoped picker
        client.

        Args:
            user_input: Unused; present to satisfy the HA flow handler
                protocol.

        Returns:
            A ConfigFlowResult delegating to :meth:`async_step_stop`.
        """
        return await self.async_step_stop()

    @callback
    def async_remove(self) -> None:
        """Close the session-scoped picker client when the flow is discarded.

        Home Assistant calls this once a flow is removed from the
        in-progress list, whether it finished, aborted, or was abandoned.
        The close itself is scheduled as a background task since this hook
        is synchronous.
        """
        if self._picker_client is not None:
            client, self._picker_client = self._picker_client, None
            self.hass.async_create_task(client.async_close())


class TfiLiveConfigFlow(_SensorPickerFlow, config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the multi-step config flow for TFI Live.

    Step 1 collects integration-level settings (API key and feed URLs).
    Step 2 collects one or more sensors via the stop/route picker and can be
    repeated to add multiple sensors before the entry is finalised.
    """

    VERSION = 1
    # Minor version 2: trip update URLs no longer carry format=json (#99);
    # async_migrate_entry in __init__ strips it from stored entries.
    MINOR_VERSION = 2

    def __init__(self) -> None:
        """Initialise the config flow with an empty staged config."""
        self._config: dict[str, Any] = {}
        self._entry: config_entries.ConfigEntry | None = None
        self._picker_client: StaticGtfsPickerClient | None = None
        self._pending_stop_id: str | None = None

    def _static_gtfs_url(self) -> str:
        """Return the static GTFS feed URL staged from step 1.

        Returns:
            The static GTFS feed URL entered on the step 1 form.
        """
        return str(self._config[CONF_STATIC_GTFS_URL])

    def _append_sensor(self, sensor_config: dict[str, Any]) -> None:
        """Append a completed sensor config to the staged entry data.

        Args:
            sensor_config: The sensor configuration to stage for saving.
        """
        self._config[CONF_SENSORS].append(sensor_config)

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> TfiLiveOptionsFlowHandler:
        """Return the options flow handler for this integration.

        Args:
            config_entry: The existing config entry for which options are
                being managed.

        Returns:
            A new :class:`TfiLiveOptionsFlowHandler` instance.
        """
        return TfiLiveOptionsFlowHandler()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle step 1 — integration-level settings.

        Presents fields for the API key and the two feed URLs. The URLs are
        pre-filled with the NTA defaults. After field validation passes, a
        lightweight probe is made to the trip update feed to verify the API
        key before advancing to step 2.

        Args:
            user_input: Form data submitted by the user, or ``None`` when
                the form is first rendered.

        Returns:
            A ConfigFlowResult that either re-shows the form with field errors
            or advances to :meth:`async_step_stop`.
        """
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        errors: dict[str, str] = {}

        if user_input is not None:
            api_key: str = user_input.get(CONF_API_KEY, "").strip()
            trip_update_url: str = user_input.get(CONF_TRIP_UPDATE_URL, "").strip()
            static_gtfs_url: str = user_input.get(CONF_STATIC_GTFS_URL, "").strip()
            num_departures_raw: str = str(
                user_input.get(CONF_NUM_DEPARTURES, DEFAULT_NUM_DEPARTURES)
            ).strip()

            if not api_key:
                errors[CONF_API_KEY] = "required"

            if not errors.get(CONF_TRIP_UPDATE_URL):
                try:
                    _url_validator(trip_update_url)
                except vol.Invalid:
                    errors[CONF_TRIP_UPDATE_URL] = "invalid_url"

            if not errors.get(CONF_STATIC_GTFS_URL):
                try:
                    _url_validator(static_gtfs_url)
                except vol.Invalid:
                    errors[CONF_STATIC_GTFS_URL] = "invalid_url"

            num_departures = DEFAULT_NUM_DEPARTURES
            try:
                num_departures = _validate_num_departures(num_departures_raw)
            except vol.Invalid:
                errors[CONF_NUM_DEPARTURES] = "invalid_num_departures"

            if not errors:
                await _probe_feed(self.hass, trip_update_url, api_key, errors)

            if not errors:
                self._config = {
                    CONF_API_KEY: api_key,
                    CONF_TRIP_UPDATE_URL: trip_update_url,
                    CONF_STATIC_GTFS_URL: static_gtfs_url,
                    CONF_NUM_DEPARTURES: num_departures,
                    CONF_SENSORS: [],
                }
                return await self.async_step_stop()

        schema = vol.Schema(
            {
                vol.Required(CONF_API_KEY): str,
                vol.Required(
                    CONF_TRIP_UPDATE_URL, default=DEFAULT_TRIP_UPDATE_URL
                ): str,
                vol.Required(
                    CONF_STATIC_GTFS_URL, default=DEFAULT_STATIC_GTFS_URL
                ): str,
                vol.Optional(
                    CONF_NUM_DEPARTURES, default=str(DEFAULT_NUM_DEPARTURES)
                ): str,
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the 'Finish' menu choice and create the config entry.

        Args:
            user_input: Unused; present to satisfy the HA flow handler
                protocol.

        Returns:
            A ConfigFlowResult that creates the config entry with all staged data.
        """
        return self.async_create_entry(title="TFI Live", data=self._config)

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Handle the start of a re-authentication flow.

        Called by HA when the coordinator signals that the current API key
        has been rejected (HTTP 401). Stores a reference to the existing
        config entry and advances to the confirmation form.

        Args:
            entry_data: The existing config entry data; unused directly but
                required by the HA flow protocol.

        Returns:
            A ConfigFlowResult delegating to :meth:`async_step_reauth_confirm`.
        """
        self._entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Present the re-auth form and process the new API key.

        Only the ``api_key`` field is shown; all other configuration values
        are preserved unchanged from the existing entry.

        Args:
            user_input: Form data submitted by the user, or ``None`` when
                the form is first rendered.

        Returns:
            A ConfigFlowResult that re-shows the form with errors, or aborts with
            ``reauth_successful`` after updating the entry and reloading it.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            api_key: str = user_input.get(CONF_API_KEY, "").strip()

            if not api_key:
                errors[CONF_API_KEY] = "required"

            if not errors and self._entry is not None:
                updated_data = {**self._entry.data, CONF_API_KEY: api_key}
                self.hass.config_entries.async_update_entry(
                    self._entry, data=updated_data
                )
                await self.hass.config_entries.async_reload(self._entry.entry_id)
                return self.async_abort(reason="reauth_successful")

        schema = vol.Schema(
            {
                vol.Required(CONF_API_KEY): str,
            }
        )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle a reconfiguration flow to update API key and feed URLs.

        Preserves the existing sensor list. Re-probes the feed after
        the new credentials are entered.

        Args:
            user_input: Form data submitted by the user, or None on first render.

        Returns:
            A ConfigFlowResult that re-shows the form with errors, or updates
            the entry and reloads it on success.
        """
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            api_key: str = user_input.get(CONF_API_KEY, "").strip()
            trip_update_url: str = user_input.get(CONF_TRIP_UPDATE_URL, "").strip()
            static_gtfs_url: str = user_input.get(CONF_STATIC_GTFS_URL, "").strip()
            num_departures_raw: str = str(
                user_input.get(CONF_NUM_DEPARTURES, DEFAULT_NUM_DEPARTURES)
            ).strip()

            if not api_key:
                errors[CONF_API_KEY] = "required"

            if not errors.get(CONF_TRIP_UPDATE_URL):
                try:
                    _url_validator(trip_update_url)
                except vol.Invalid:
                    errors[CONF_TRIP_UPDATE_URL] = "invalid_url"

            if not errors.get(CONF_STATIC_GTFS_URL):
                try:
                    _url_validator(static_gtfs_url)
                except vol.Invalid:
                    errors[CONF_STATIC_GTFS_URL] = "invalid_url"

            num_departures = DEFAULT_NUM_DEPARTURES
            try:
                num_departures = _validate_num_departures(num_departures_raw)
            except vol.Invalid:
                errors[CONF_NUM_DEPARTURES] = "invalid_num_departures"

            if not errors:
                await _probe_feed(self.hass, trip_update_url, api_key, errors)

            if not errors:
                return self.async_update_reload_and_abort(
                    entry,
                    data={
                        **entry.data,
                        CONF_API_KEY: api_key,
                        CONF_TRIP_UPDATE_URL: trip_update_url,
                        CONF_STATIC_GTFS_URL: static_gtfs_url,
                        CONF_NUM_DEPARTURES: num_departures,
                    },
                    reason="reconfigure_successful",
                )

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_API_KEY,
                    default=entry.data.get(CONF_API_KEY, ""),
                ): str,
                vol.Required(
                    CONF_TRIP_UPDATE_URL,
                    default=entry.data.get(
                        CONF_TRIP_UPDATE_URL, DEFAULT_TRIP_UPDATE_URL
                    ),
                ): str,
                vol.Required(
                    CONF_STATIC_GTFS_URL,
                    default=entry.data.get(
                        CONF_STATIC_GTFS_URL, DEFAULT_STATIC_GTFS_URL
                    ),
                ): str,
                vol.Optional(
                    CONF_NUM_DEPARTURES,
                    default=str(
                        entry.data.get(CONF_NUM_DEPARTURES, DEFAULT_NUM_DEPARTURES)
                    ),
                ): str,
            }
        )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=schema,
            errors=errors,
        )


class TfiLiveOptionsFlowHandler(_SensorPickerFlow, config_entries.OptionsFlow):
    """Options flow for managing sensors after initial setup.

    Mirrors the sensor-addition steps from the main config flow but
    operates on the existing config entry data to append new sensors.
    Removing existing sensors is not supported in this version.
    """

    def __init__(self) -> None:
        """Initialise the options flow with an empty staged sensor list."""
        self._new_sensors: list[dict[str, Any]] = []
        self._picker_client: StaticGtfsPickerClient | None = None
        self._pending_stop_id: str | None = None

    def _static_gtfs_url(self) -> str:
        """Return the static GTFS feed URL from the existing config entry.

        Returns:
            The static GTFS feed URL already stored on the config entry.
        """
        return str(self.config_entry.data[CONF_STATIC_GTFS_URL])

    def _append_sensor(self, sensor_config: dict[str, Any]) -> None:
        """Append a completed sensor config to the staged new-sensor list.

        Args:
            sensor_config: The sensor configuration to stage for saving.
        """
        self._new_sensors.append(sensor_config)

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Start the options flow by presenting the stop-search step.

        Args:
            user_input: Unused; delegated immediately to
                :meth:`async_step_stop`.

        Returns:
            A ConfigFlowResult delegating to :meth:`async_step_stop`.
        """
        return await self.async_step_stop()

    async def async_step_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the 'Finish' menu choice and persist new sensors.

        Merges the newly added sensors into the existing entry data and
        creates the options entry to trigger a coordinator reload.

        Args:
            user_input: Unused; present to satisfy the HA flow handler
                protocol.

        Returns:
            A ConfigFlowResult that creates the options entry with merged
            sensor data.
        """
        existing_sensors: list[dict[str, Any]] = list(
            self.config_entry.data.get(CONF_SENSORS, [])
        )
        updated_data = {
            **self.config_entry.data,
            CONF_SENSORS: existing_sensors + self._new_sensors,
        }
        return self.async_create_entry(title="", data=updated_data)
