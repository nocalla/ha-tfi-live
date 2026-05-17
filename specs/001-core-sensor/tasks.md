---
title: "001 — Core Sensor Integration — Tasks"
spec: specs/001-core-sensor/spec.md
plan: specs/001-core-sensor/plan.md
created: 2026-05-17
---

# Tasks — Core Sensor Integration

Tasks are ordered by dependency. A task may only begin when all tasks listed under its **Depends on** field are complete.

---

## T-001 — Scaffold the repository and integration package

**What done looks like**

The following files exist and are syntactically valid:

- `pyproject.toml` — declares the project, sets `hatchling` as build backend, lists dev dependencies (`pytest`, `pytest-cov`, `pytest-homeassistant-custom-component`, `aioresponses`, `ruff`, `bandit`, `pydocstyle`), and sets `ruff` to 88-char line length.
- `custom_components/tfi_live/__init__.py` — empty stub (no logic yet).
- `custom_components/tfi_live/manifest.json` — valid HA manifest with domain `tfi_live`, version `0.1.0`, `config_flow: true`, `iot_class: cloud_polling`, `requirements: ["pandas>=2.0.0"]`, `codeowners: ["@nocalla"]`.
- `custom_components/tfi_live/const.py` — empty stub.
- `custom_components/tfi_live/coordinator.py` — empty stub.
- `custom_components/tfi_live/sensor.py` — empty stub.
- `custom_components/tfi_live/config_flow.py` — empty stub.
- `custom_components/tfi_live/static_gtfs.py` — empty stub.
- `tests/__init__.py` — empty.
- `.gitignore` — ignores `__pycache__`, `.venv`, `*.pyc`, `.pytest_cache`, `dist/`, `.ruff_cache/`.
- `uv` environment can be created and `pytest` runs (0 collected tests, no errors).

**Acceptance criteria covered:** Prerequisite for all; establishes structure required by CONSTITUTION §4 (modular layout), §6 (hatchling build backend), §2 (uv tooling).

**Depends on:** none

---

## T-002 — Implement `const.py`

**What done looks like**

`custom_components/tfi_live/const.py` defines, at minimum:

- `DOMAIN = "tfi_live"`
- `DEFAULT_TRIP_UPDATE_URL` — the default NTA GTFS-RT trip updates URL from spec §Configuration Step 1.
- `DEFAULT_STATIC_GTFS_URL` — the default static GTFS zip URL from spec §Configuration Step 1.
- Config key constants for all config flow fields (`CONF_API_KEY`, `CONF_TRIP_UPDATE_URL`, `CONF_STATIC_GTFS_URL`, `CONF_STOP_ID`, `CONF_ROUTE_ID`, `CONF_DIRECTION_ID`, `CONF_OPERATOR_ID`, `CONF_SENSORS`).
- Attribute key constants for all sensor attributes (`ATTR_STOP_ID`, `ATTR_ROUTE_ID`, `ATTR_DIRECTION_ID`, `ATTR_OPERATOR_ID`, `ATTR_DEPARTURES`, `ATTR_LAST_UPDATED`).
- Departure dict key constants (`DEP_SCHEDULED_TIME`, `DEP_REALTIME_TIME`, `DEP_DELAY_MINUTES`, `DEP_TRIP_ID`, `DEP_ROUTE_NAME`).
- `UPDATE_INTERVAL_SECONDS = 60`
- `AVAILABILITY_WINDOW_SECONDS = 180`
- `STATIC_GTFS_REFRESH_HOURS = 24`
- `MAX_DEPARTURES = 3`

All constants are typed (`Final[str]`, `Final[int]` etc.) and have Google-style docstrings or inline comments.

**Acceptance criteria covered:** Foundation for AC 3, 4, 5, 6, 14, 24, 25; directly referenced by all other modules.

**Depends on:** T-001

---

## T-003 — Implement `static_gtfs.py` — download, parse, and cache static GTFS data

**What done looks like**

`custom_components/tfi_live/static_gtfs.py` contains a `StaticGtfsCache` class that:

- Accepts `static_gtfs_url: str` and an `aiohttp.ClientSession` (or HA session reference) at construction.
- `async_load()` — downloads the zip from the configured URL using `aiohttp`, extracts in-memory (no disk writes), and builds pandas DataFrames for: `stops`, `routes`, `trips`, `stop_times` (at minimum); gracefully handles download failure and parse failure by setting `self.available = False` and emitting a WARNING log (exactly once per failure event, not once per call).
- `get_scheduled_departures(stop_id, route_id, direction_id, operator_id, target_date)` — returns a list of `(trip_id, scheduled_time_str, route_name)` tuples sorted ascending by scheduled time; returns an empty list when `self.available = False`.
- `async_refresh_if_stale()` — re-calls `async_load()` if more than 24 hours have elapsed since the last successful load; otherwise is a no-op.
- `self.available: bool` — `True` only after a successful load.
- `self._loaded_at: datetime | None` — timestamp of the last successful load.

All methods have type hints and Google-style docstrings.

**Acceptance criteria covered:** AC 9, 10 (graceful degradation when static GTFS unavailable); CONSTITUTION §5 (static GTFS as in-memory cache, never written to disk).

**Depends on:** T-002

---

## T-004 — Implement `coordinator.py` — GTFS-RT fetch and parsing

**What done looks like**

`custom_components/tfi_live/coordinator.py` contains `TfiLiveCoordinator`, a subclass of `DataUpdateCoordinator[dict]`, that:

- Accepts `hass`, the config entry, and a `StaticGtfsCache` reference at construction.
- Sets `update_interval = timedelta(seconds=60)`.
- `_async_update_data()` fetches the GTFS-RT trip updates JSON from the configured URL using `aiohttp` (via HA's session helper) with the `x-api-key` header set to the configured API key.
- Parses the JSON response into a `dict` keyed by `(stop_id, route_id, direction_id, operator_id)` → `list[dict]` of raw departure data (at minimum: `trip_id`, raw arrival/departure delay, raw scheduled time).
- On HTTP 401: calls `self.config_entry.async_start_reauth(self.hass)`, raises `ConfigEntryAuthFailed`, emits one ERROR log.
- On HTTP 4xx (other) / 5xx / timeout: raises `UpdateFailed`, emits one WARNING log.
- On unparseable JSON: raises `UpdateFailed`, emits one ERROR log.
- Log deduplication: a given error condition (same HTTP status code or same parse-failure category) is logged at most once per distinct failure run; once the coordinator recovers and fails again, the error is logged again.
- Stores `_last_successful_fetch: datetime | None` updated on each successful `_async_update_data` completion.

All methods have type hints and Google-style docstrings.

**Acceptance criteria covered:** AC 14 (60-second interval), AC 15, 16 (HTTP error handling and no repeated logs), AC 17 (401 re-auth trigger), AC 26 (invalid JSON / ERROR log).

**Depends on:** T-003

---

## T-005 — Implement `sensor.py` — sensor entity and departure merging logic

**What done looks like**

`custom_components/tfi_live/sensor.py` contains `TfiLiveSensor`, a subclass of `SensorEntity`, that:

- Accepts the coordinator and a sensor config dict at construction.
- `unique_id`: constructed as `f"{entry.entry_id}_{stop_id}_{route_id}_{direction_id or ''}_{operator_id or ''}"` — stable across HA restarts; distinct for sensors differing only in `direction_id` or `operator_id`.
- `native_unit_of_measurement = "min"`.
- `available`: `True` when `coordinator.last_update_success` is `True` and `coordinator._last_successful_fetch` is within the last 180 seconds; `False` otherwise.
- `native_value`: `floor(minutes_to_next_departure)` as an `int`, or `None` when no matching departure exists or the sensor is unavailable. Uses real-time departure time when available; falls back to scheduled time from static GTFS when real-time is absent.
- Departure merging: joins coordinator RT data with `StaticGtfsCache.get_scheduled_departures()` on `trip_id`; produces at most 3 departure dicts sorted ascending by effective departure time (real-time if present, scheduled otherwise); each dict has exactly the keys `scheduled_time`, `realtime_time`, `delay_minutes`, `trip_id`, `route_name` with types per spec §Entity Model.
- `extra_state_attributes`: returns dict with keys `stop_id`, `route_id`, `direction_id`, `operator_id`, `departures`, `last_updated`. When `available = False`, all values are `None`.
- `last_updated` attribute: ISO 8601 string of `coordinator._last_successful_fetch`; `None` if no successful fetch yet.
- `async_setup_entry` in `sensor.py`: creates one `TfiLiveSensor` per entry in the `sensors` list in config entry data; calls `async_add_entities`.

All methods have type hints and Google-style docstrings.

**Acceptance criteria covered:** AC 1, 2, 3, 4, 5, 6, 7, 7a, 8, 11, 12, 13, 24, 25.

**Depends on:** T-004

---

## T-006 — Implement `config_flow.py` — step 1, step 2, and re-auth flow

**What done looks like**

`custom_components/tfi_live/config_flow.py` contains:

- `TfiLiveConfigFlow(ConfigFlow)`:
  - `async_step_user`: presents step 1 form (fields: `api_key`, `trip_update_url` pre-filled with default, `static_gtfs_url` pre-filled with default). Validates: `api_key` not empty; `trip_update_url` and `static_gtfs_url` are parseable URLs with scheme and host. On validation failure, re-renders the form with the appropriate field error. No live API call is made. On success, stores values and advances to `async_step_sensor`.
  - `async_step_sensor`: presents step 2 form (fields: `name`, `stop_id`, `route_id`, `direction_id` optional, `operator_id` optional). Validates: `name`, `stop_id`, `route_id` not empty; if `direction_id` provided, must be `0` or `1`. On validation failure, re-renders with field error. On success, appends sensor config to `sensors` list. Presents choice to add another sensor or finish.
  - On finish: calls `self.async_create_entry(title=..., data=...)`.
  - `async_step_add_sensor` (options flow or re-entry): allows adding sensors after initial setup.
- `TfiLiveReauthFlow(FlowHandler)`:
  - `async_step_reauth`: presents a form with only `api_key`. On submit, updates the config entry data, replacing only `api_key`; all other keys are preserved. Calls `self.hass.config_entries.async_reload(self.context["entry_id"])`.
- No live API calls anywhere in the config flow.

All methods have type hints and Google-style docstrings.

**Acceptance criteria covered:** AC 17 (re-auth flow), AC 18 (re-auth preserves config), AC 19, 20, 21, 22, 23.

**Depends on:** T-002

---

## T-007 — Implement `__init__.py` — integration setup and teardown

**What done looks like**

`custom_components/tfi_live/__init__.py` implements:

- `async_setup_entry(hass, entry)`:
  - Instantiates `StaticGtfsCache` with the configured URL and the HA aiohttp session.
  - Calls `await cache.async_load()` (failures are caught and logged; setup continues regardless — AC 10).
  - Instantiates `TfiLiveCoordinator` with `hass`, the config entry, and the cache.
  - Calls `await coordinator.async_config_entry_first_refresh()`.
  - Stores the coordinator on `hass.data[DOMAIN][entry.entry_id]`.
  - Calls `await hass.config_entries.async_forward_entry_setups(entry, ["sensor"])`.
  - Returns `True`.
- `async_unload_entry(hass, entry)`:
  - Calls `await hass.config_entries.async_unload_platforms(entry, ["sensor"])`.
  - Removes `hass.data[DOMAIN][entry.entry_id]`.
  - Returns the result of the unload call.

All methods have type hints and Google-style docstrings.

**Acceptance criteria covered:** AC 1 (entity creation wired end-to-end), AC 10 (static GTFS failure does not abort setup).

**Depends on:** T-005, T-006

---

## T-008 — Write tests for `static_gtfs.py`

**What done looks like**

`tests/test_static_gtfs.py` contains pytest tests that, without any live network calls:

- Provide a minimal synthetic GTFS zip in `tests/fixtures/gtfs_minimal.zip` (or generated in-memory within the test) containing `stops.txt`, `routes.txt`, `trips.txt`, `stop_times.txt`, `calendar.txt`.
- Test that `async_load()` with a successful mocked download sets `cache.available = True` and populates DataFrames.
- Test that `get_scheduled_departures()` returns correctly typed, correctly sorted results for a known stop/route combination in the synthetic data.
- Test that `get_scheduled_departures()` returns an empty list when `cache.available = False`.
- Test that `async_load()` with a simulated HTTP error sets `cache.available = False` and emits exactly one WARNING log (not two on two calls with the same error).
- Test that `async_load()` with a simulated parse error sets `cache.available = False`.
- Test that `async_refresh_if_stale()` does not re-download when called within 24 hours of a successful load.
- Test that `async_refresh_if_stale()` does re-download when called more than 24 hours after a successful load.

Coverage of `static_gtfs.py` reaches 100%.

**Acceptance criteria covered:** AC 9, 10.

**Depends on:** T-003

---

## T-009 — Write tests for `coordinator.py`

**What done looks like**

`tests/test_coordinator.py` contains pytest tests using `pytest-homeassistant-custom-component` fixtures and `aioresponses` (or `unittest.mock`) to mock HTTP calls:

- Test that a successful 200 response with valid JSON is parsed into the expected `dict` structure keyed by `(stop_id, route_id, direction_id, operator_id)`.
- Test that `update_interval` is `timedelta(seconds=60)`.
- Test that an HTTP 500 response raises `UpdateFailed` and emits exactly one WARNING log; a second consecutive 500 does not emit a second WARNING log.
- Test that an HTTP 401 response raises `ConfigEntryAuthFailed`, emits one ERROR log, and calls `async_start_reauth`.
- Test that an unparseable JSON response (200 with garbage body) raises `UpdateFailed` and emits one ERROR log.
- Test that `_last_successful_fetch` is updated on a successful call and is `None` before any call.
- Test that a timeout raises `UpdateFailed` and emits one WARNING log.

Coverage of `coordinator.py` reaches 100%.

**Acceptance criteria covered:** AC 14, 15, 16, 17, 26.

**Depends on:** T-004

---

## T-010 — Write tests for `sensor.py` — state, attributes, and availability

**What done looks like**

`tests/test_sensor.py` contains pytest tests using HA test fixtures, a minimal mocked coordinator, and a minimal mocked `StaticGtfsCache`:

- Test AC 1: given N sensor configs in the config entry, exactly N `TfiLiveSensor` entities are registered.
- Test AC 2: no `device_tracker` entities are created.
- Test AC 3: `native_value` equals `floor(T)` for fractional T (e.g. T=2.9 → 2, T=-1.3 → -1).
- Test AC 4: `native_value` is `-2` when departure was 2 min 45 sec in the past.
- Test AC 5: when no real-time data exists for a trip, `native_value` is calculated from scheduled time and `realtime_time` in the departure dict is `None`.
- Test AC 6: given 5 matching departures, `departures` attribute contains exactly 3 entries each with exactly the keys `scheduled_time`, `realtime_time`, `delay_minutes`, `trip_id`, `route_name`.
- Test AC 7: given 1 matching departure, `departures` contains exactly 1 entry.
- Test AC 7a: given departures A (scheduled 09:10, no RT), B (RT 09:05), C (RT 09:08), `departures` order is [B, C, A].
- Test AC 8: given 0 matching departures, `departures` is `[]`, `native_value` is `None`, `available` is `True`.
- Test AC 9: given `cache.available = False`, `scheduled_time` and `route_name` are `None` in all departure dicts; `available` is `True` when coordinator is healthy.
- Test AC 11: given `_last_successful_fetch` > 3 minutes ago, `available` is `False`.
- Test AC 12: when `available = False`, `native_value` is `None` and all attributes are `None`.
- Test AC 13: given a successful fetch within 3 minutes, `available` is `True`.
- Test AC 24: `stop_id`, `route_id`, `direction_id`, `operator_id` attributes match config values exactly.
- Test AC 25: `last_updated` is a valid ISO 8601 string, within the interval of a successful update call.
- Test unique ID stability and distinctness: two sensors with same `stop_id`/`route_id` but different `direction_id` have different `unique_id`.

Coverage of `sensor.py` reaches 100%.

**Acceptance criteria covered:** AC 1, 2, 3, 4, 5, 6, 7, 7a, 8, 9, 11, 12, 13, 24, 25.

**Depends on:** T-005

---

## T-011 — Write tests for `config_flow.py`

**What done looks like**

`tests/test_config_flow.py` contains pytest tests using `pytest-homeassistant-custom-component` config flow test helpers:

- Test AC 19: submitting step 1 with empty `api_key` returns a form error on `api_key` and does not advance.
- Test AC 20: submitting step 1 with a malformed value for `trip_update_url` returns a form error on `trip_update_url`; same for `static_gtfs_url`.
- Test AC 21: submitting step 2 with empty `name`, `stop_id`, or `route_id` returns a form error on the offending field and creates no entity.
- Test AC 22: submitting step 2 with `direction_id = 2` returns a form error on `direction_id`.
- Test AC 23: after completing step 2 and creating a first sensor, the user can re-enter step 2 to add a second sensor; the resulting config entry contains exactly 2 sensors in its data.
- Test AC 18 (re-auth): after a re-auth flow submission with a new `api_key`, all other config keys (`trip_update_url`, `static_gtfs_url`, sensor configs) are unchanged.
- Test that no live API call is made at any point during step 1 or step 2 (mock HTTP client raises if called; test asserts it is never called).

Coverage of `config_flow.py` reaches 100%.

**Acceptance criteria covered:** AC 18, 19, 20, 21, 22, 23.

**Depends on:** T-006

---

## T-012 — Write end-to-end integration tests via `async_setup_entry`

**What done looks like**

`tests/test_integration.py` contains end-to-end tests that call `async_setup_entry` with a fully mocked HTTP layer:

- Test: given a valid config entry and a mocked GTFS-RT feed returning well-formed JSON, `async_setup_entry` completes without error, coordinator is stored on `hass.data`, and sensor entities are registered.
- Test AC 10: given a config entry where the static GTFS download returns HTTP 500, `async_setup_entry` still completes without error and the config entry is not marked failed.
- Test AC 17 + end-to-end: given a GTFS-RT feed that returns 401 on the first coordinator refresh after setup, `async_start_reauth` is called on the config entry.
- Test: `async_unload_entry` unregisters sensor entities and removes coordinator from `hass.data`.

Coverage contribution brings overall integration coverage to >= 95% (100% target).

**Acceptance criteria covered:** AC 1, 10, 17 (end-to-end path).

**Depends on:** T-007, T-008, T-009, T-010, T-011
