"""Config flow for the TFI Live integration.

Handles the two-step initial setup (step 1: integration-level settings;
step 2: add one or more sensors), the re-authentication flow triggered
when the GTFS-RT feed returns HTTP 401, the reconfiguration flow for
updating credentials, and the options flow for adding sensors after setup.
"""

from __future__ import annotations

import urllib.parse
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry, ConfigFlowResult
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_API_KEY,
    CONF_DIRECTION_ID,
    CONF_OPERATOR_ID,
    CONF_ROUTE_ID,
    CONF_SENSORS,
    CONF_STATIC_GTFS_URL,
    CONF_STOP_ID,
    CONF_TRIP_UPDATE_URL,
    DEFAULT_STATIC_GTFS_URL,
    DEFAULT_TRIP_UPDATE_URL,
    DOMAIN,
)

_SENSOR_NAME_KEY = "name"

# Sentinel used for the post-sensor-add menu choice.
_MENU_ADD_ANOTHER = "add_another"
_MENU_FINISH = "finish"


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


async def _probe_feed(
    hass: Any,
    trip_update_url: str,
    api_key: str,
    errors: dict[str, str],
) -> None:
    """Probe the GTFS-RT feed URL to validate the API key.

    Makes a lightweight GET request to the trip update feed with the
    supplied API key. Populates ``errors["base"]`` if the probe fails.

    Args:
        hass: The Home Assistant instance used to obtain the HTTP client session.
        trip_update_url: The GTFS-RT trip update feed URL to probe.
        api_key: The NTA API key to include in the request header.
        errors: Mutable dict of form errors; ``"base"`` will be set on failure.
    """
    session = async_get_clientsession(hass)
    try:
        async with session.get(
            trip_update_url,
            headers={"x-api-key": api_key},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 401:
                errors["base"] = "invalid_auth"
            elif resp.status >= 400:
                errors["base"] = "cannot_connect"
    except Exception:
        errors["base"] = "cannot_connect"


class TfiLiveConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the multi-step config flow for TFI Live.

    Step 1 collects integration-level settings (API key and feed URLs).
    Step 2 collects sensor-level settings (stop/route) and can be repeated
    to add multiple sensors before the entry is finalised.
    """

    VERSION = 1

    def __init__(self) -> None:
        """Initialise the config flow with an empty staged config."""
        self._config: dict[str, Any] = {}
        self._entry: config_entries.ConfigEntry | None = None

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
            or advances to :meth:`async_step_sensor`.
        """
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        errors: dict[str, str] = {}

        if user_input is not None:
            api_key: str = user_input.get(CONF_API_KEY, "").strip()
            trip_update_url: str = user_input.get(CONF_TRIP_UPDATE_URL, "").strip()
            static_gtfs_url: str = user_input.get(CONF_STATIC_GTFS_URL, "").strip()

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

            if not errors:
                await _probe_feed(self.hass, trip_update_url, api_key, errors)

            if not errors:
                self._config = {
                    CONF_API_KEY: api_key,
                    CONF_TRIP_UPDATE_URL: trip_update_url,
                    CONF_STATIC_GTFS_URL: static_gtfs_url,
                    CONF_SENSORS: [],
                }
                return await self.async_step_sensor()

        schema = vol.Schema(
            {
                vol.Required(CONF_API_KEY): str,
                vol.Required(
                    CONF_TRIP_UPDATE_URL, default=DEFAULT_TRIP_UPDATE_URL
                ): str,
                vol.Required(
                    CONF_STATIC_GTFS_URL, default=DEFAULT_STATIC_GTFS_URL
                ): str,
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_sensor(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle step 2 — add a sensor.

        Presents fields for one stop/route sensor. The step can be repeated
        to add multiple sensors. After a successful submission the user is
        offered a menu to add another sensor or finish setup.

        Args:
            user_input: Form data submitted by the user, or ``None`` when
                the form is first rendered.

        Returns:
            A ConfigFlowResult that re-shows the form with errors, shows the
            post-add menu, or (after finishing) creates the config entry.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            name: str = user_input.get(_SENSOR_NAME_KEY, "").strip()
            stop_id: str = user_input.get(CONF_STOP_ID, "").strip()
            route_id: str = user_input.get(CONF_ROUTE_ID, "").strip()
            direction_id_raw: str = user_input.get(CONF_DIRECTION_ID, "").strip()
            operator_id: str = user_input.get(CONF_OPERATOR_ID, "").strip()

            if not name:
                errors[_SENSOR_NAME_KEY] = "required"
            if not stop_id:
                errors[CONF_STOP_ID] = "required"
            if not route_id:
                errors[CONF_ROUTE_ID] = "required"

            direction_id: int | None = None
            if direction_id_raw:
                try:
                    direction_id = int(direction_id_raw)
                    if direction_id not in (0, 1):
                        errors[CONF_DIRECTION_ID] = "invalid_direction"
                        direction_id = None
                except ValueError:
                    errors[CONF_DIRECTION_ID] = "invalid_direction"

            if not errors:
                sensor_config: dict[str, Any] = {
                    _SENSOR_NAME_KEY: name,
                    CONF_STOP_ID: stop_id,
                    CONF_ROUTE_ID: route_id,
                    CONF_DIRECTION_ID: direction_id,
                    CONF_OPERATOR_ID: operator_id or None,
                }
                self._config[CONF_SENSORS].append(sensor_config)

                return self.async_show_menu(
                    step_id="sensor",
                    menu_options=[_MENU_ADD_ANOTHER, _MENU_FINISH],
                )

        schema = vol.Schema(
            {
                vol.Required(_SENSOR_NAME_KEY): str,
                vol.Required(CONF_STOP_ID): str,
                vol.Required(CONF_ROUTE_ID): str,
                vol.Optional(CONF_DIRECTION_ID, default=""): str,
                vol.Optional(CONF_OPERATOR_ID, default=""): str,
            }
        )

        return self.async_show_form(
            step_id="sensor",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_add_another(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the 'Add another sensor' menu choice.

        Loops back to :meth:`async_step_sensor` so the user can configure
        an additional stop/route without re-entering step 1 values.

        Args:
            user_input: Unused; present to satisfy the HA flow handler
                protocol.

        Returns:
            A ConfigFlowResult delegating to :meth:`async_step_sensor`.
        """
        return await self.async_step_sensor()

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
            }
        )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=schema,
            errors=errors,
        )


class TfiLiveOptionsFlowHandler(config_entries.OptionsFlow):
    """Options flow for managing sensors after initial setup.

    Mirrors the sensor-addition steps from the main config flow but
    operates on the existing config entry data to append new sensors.
    Removing existing sensors is not supported in this version.
    """

    def __init__(self) -> None:
        """Initialise the options flow with an empty staged sensor list."""
        self._new_sensors: list[dict[str, Any]] = []

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Start the options flow by presenting the add-sensor form.

        Args:
            user_input: Unused; delegated immediately to
                :meth:`async_step_sensor`.

        Returns:
            A ConfigFlowResult delegating to :meth:`async_step_sensor`.
        """
        return await self.async_step_sensor()

    async def async_step_sensor(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle adding a new sensor in the options flow.

        Presents the same stop/route/direction/operator fields as step 2 of
        the initial config flow. After a successful submission the user is
        offered a menu to add another sensor or finish.

        Args:
            user_input: Form data submitted by the user, or ``None`` when
                the form is first rendered.

        Returns:
            A ConfigFlowResult that re-shows the form with errors, shows the
            post-add menu, or finishes the options flow.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            name: str = user_input.get(_SENSOR_NAME_KEY, "").strip()
            stop_id: str = user_input.get(CONF_STOP_ID, "").strip()
            route_id: str = user_input.get(CONF_ROUTE_ID, "").strip()
            direction_id_raw: str = user_input.get(CONF_DIRECTION_ID, "").strip()
            operator_id: str = user_input.get(CONF_OPERATOR_ID, "").strip()

            if not name:
                errors[_SENSOR_NAME_KEY] = "required"
            if not stop_id:
                errors[CONF_STOP_ID] = "required"
            if not route_id:
                errors[CONF_ROUTE_ID] = "required"

            direction_id: int | None = None
            if direction_id_raw:
                try:
                    direction_id = int(direction_id_raw)
                    if direction_id not in (0, 1):
                        errors[CONF_DIRECTION_ID] = "invalid_direction"
                        direction_id = None
                except ValueError:
                    errors[CONF_DIRECTION_ID] = "invalid_direction"

            if not errors:
                sensor_config: dict[str, Any] = {
                    _SENSOR_NAME_KEY: name,
                    CONF_STOP_ID: stop_id,
                    CONF_ROUTE_ID: route_id,
                    CONF_DIRECTION_ID: direction_id,
                    CONF_OPERATOR_ID: operator_id or None,
                }
                self._new_sensors.append(sensor_config)

                return self.async_show_menu(
                    step_id="sensor",
                    menu_options=[_MENU_ADD_ANOTHER, _MENU_FINISH],
                )

        schema = vol.Schema(
            {
                vol.Required(_SENSOR_NAME_KEY): str,
                vol.Required(CONF_STOP_ID): str,
                vol.Required(CONF_ROUTE_ID): str,
                vol.Optional(CONF_DIRECTION_ID, default=""): str,
                vol.Optional(CONF_OPERATOR_ID, default=""): str,
            }
        )

        return self.async_show_form(
            step_id="sensor",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_add_another(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the 'Add another sensor' menu choice in the options flow.

        Args:
            user_input: Unused; present to satisfy the HA flow handler
                protocol.

        Returns:
            A ConfigFlowResult delegating to :meth:`async_step_sensor`.
        """
        return await self.async_step_sensor()

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
