"""Config flow for the TFI Live integration.

Handles the two-step initial setup (step 1: integration-level settings;
step 2: add one or more sensors) and the re-authentication flow triggered
when the GTFS-RT feed returns HTTP 401.
"""

from __future__ import annotations

import urllib.parse
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult

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

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle step 1 — integration-level settings.

        Presents fields for the API key and the two feed URLs. The URLs are
        pre-filled with the NTA defaults. No live API call is made here;
        the API key is validated at runtime by the coordinator.

        Args:
            user_input: Form data submitted by the user, or ``None`` when
                the form is first rendered.

        Returns:
            A FlowResult that either re-shows the form with field errors or
            advances to :meth:`async_step_sensor`.
        """
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
            A FlowResult that re-shows the form with errors, shows the
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
            A FlowResult delegating to :meth:`async_step_sensor`.
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
            A FlowResult that creates the config entry with all staged data.
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
            A FlowResult delegating to :meth:`async_step_reauth_confirm`.
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
            A FlowResult that re-shows the form with errors, or aborts with
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
