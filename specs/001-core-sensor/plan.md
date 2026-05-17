---
title: "001 — Core Sensor Integration — Implementation Plan"
status: draft
created: 2026-05-17
spec: specs/001-core-sensor/spec.md
---

# Implementation Plan — Core Sensor Integration

## Stack Decisions

- **GTFS-RT parsing**: `gtfs-realtime-bindings>=1.0.0` — Google's official protobuf bindings for GTFS-RT. The NTA feed returns `format=json` but the URL parameter is cosmetic; the library handles protobuf decoding. Alternatively, request `format=json` and parse raw JSON — simpler dependency. **Decision: use raw JSON parsing** (`requests` + standard `json`) to avoid a protobuf compilation dependency in the HA environment. The NTA v2 API genuinely returns JSON when `?format=json` is appended; validate this assumption against the live feed.
- **Static GTFS**: `pandas` for in-memory DataFrame processing; `requests` for zip download; standard `zipfile` + `io` for in-memory extraction (never write to disk).
- **HTTP**: `aiohttp` (HA's built-in async HTTP client via `homeassistant.helpers.aiohttp_client`) — do not use `requests` for runtime polling (blocking). Use `requests` only for the standalone test script.
- **HA version target**: current release (2026.5.x); use `DataUpdateCoordinator` from `homeassistant.helpers.update_coordinator`.

## Component Layout

```
custom_components/tfi_live/
├── __init__.py          — async_setup_entry, async_unload_entry
├── config_flow.py       — ConfigFlow (step 1 + step 2 add-sensor), ReauthFlow
├── coordinator.py       — TfiLiveCoordinator (DataUpdateCoordinator subclass)
├── sensor.py            — TfiLiveSensor (SensorEntity)
├── static_gtfs.py       — StaticGtfsCache (download, parse, daily refresh)
├── const.py             — domain name, default URLs, attribute key names, config key names
└── manifest.json        — domain, version, requirements, codeowners
```

No other files in `custom_components/tfi_live/`. Tests live in `tests/`.

## Data Flow

```
Config entry
    │
    ├─ async_setup_entry
    │       ├─ create StaticGtfsCache → download zip → parse DataFrames
    │       ├─ create TfiLiveCoordinator (holds cache reference)
    │       └─ coordinator.async_config_entry_first_refresh()
    │
    └─ async_setup_entry calls async_forward_entry_setups("sensor")
            └─ sensor.py async_setup_entry
                    └─ for each sensor config → create TfiLiveSensor(coordinator, sensor_config)

Every 60 seconds:
    TfiLiveCoordinator._async_update_data()
        ├─ fetch trip updates JSON from NTA API (aiohttp, x-api-key header)
        ├─ parse JSON into list of TripUpdate dicts
        ├─ for each TripUpdate: extract stop_time_updates for relevant stops
        └─ return dict keyed by (stop_id, route_id, direction_id, operator_id) → list[DepartureData]

TfiLiveSensor.native_value:
    ├─ look up coordinator.data[(stop_id, route_id, ...)]
    ├─ merge with StaticGtfsCache for scheduled times + route names
    ├─ sort by effective time (realtime ?? scheduled)
    ├─ take first 3
    └─ return floor(minutes_to_first_departure) or None

Once per 24 hours:
    StaticGtfsCache._refresh_if_stale()
        └─ download + re-parse static GTFS zip
```

## Key Implementation Details

### TfiLiveCoordinator

- Subclasses `DataUpdateCoordinator[dict]`
- `update_interval = timedelta(seconds=60)`
- `_async_update_data` fetches JSON, parses, returns `dict[(stop_id, route_id, direction_id, operator_id), list[DepartureData]]`
- On HTTP 401: call `self.config_entry.async_start_reauth(self.hass)` then raise `ConfigEntryAuthFailed`
- On other HTTP errors: raise `UpdateFailed` (HA handles retry)
- On parse error: raise `UpdateFailed`
- Log deduplication: track `_last_error_key` (e.g. hash of error type + status code); only log if the error key has changed since last emission

### StaticGtfsCache

- Initialised with `static_gtfs_url`
- `async_load()` — downloads zip via aiohttp, extracts in-memory, builds DataFrames: `stops`, `routes`, `trips`, `stop_times`, `calendar`, `calendar_dates`
- `get_scheduled_departures(stop_id, route_id, direction_id, operator_id, target_date)` — returns list of `(trip_id, scheduled_time, route_name)` sorted by time
- `_refresh_if_stale()` — compares `datetime.now()` to `_loaded_at`; re-calls `async_load()` if > 24 hours since last successful load
- Failure mode: if load fails, `self.available = False`; all lookups return empty list; caller checks `cache.available`

### TfiLiveSensor

- `unique_id`: `f"{entry.entry_id}_{stop_id}_{route_id}_{direction_id or ''}_{operator_id or ''}"`
- `native_value`: minutes (int) to next departure, or `None`
- `native_unit_of_measurement`: `"min"`
- `available`: True if `coordinator.last_update_success` and last successful fetch within 3 minutes
- `extra_state_attributes`: returns full dict with `stop_id`, `route_id`, `direction_id`, `operator_id`, `departures`, `last_updated`
- Departure merging: join coordinator RT data with static cache data on `trip_id`; where RT data is absent for a trip, use scheduled time as fallback for state calculation

### Config Flow

- `ConfigFlow.async_step_user`: presents step 1 form (api_key, trip_update_url, static_gtfs_url with defaults pre-filled); validates fields; stores in `self._config`
- `ConfigFlow.async_step_sensor`: presents step 2 form (name, stop_id, route_id, direction_id, operator_id); appends sensor config to `self._config["sensors"]` list; offers "Add another" vs "Finish"
- `ReauthFlow.async_step_reauth`: presents api_key-only form; on submit, updates config entry data preserving all other keys
- No live API calls during config flow

### manifest.json

```json
{
  "domain": "tfi_live",
  "name": "TFI Live",
  "version": "0.1.0",
  "requirements": ["requests>=2.28.0", "pandas>=2.0.0"],
  "dependencies": [],
  "codeowners": ["@nocalla"],
  "iot_class": "cloud_polling",
  "config_flow": true,
  "documentation": ""
}
```

Note: `aiohttp` is a HA core dependency; it does not appear in `requirements`. `pandas` and `requests` (for test script) are explicit. Re-evaluate whether `requests` is needed at runtime or only in tests.

## Testing Strategy

- Use `pytest-homeassistant-custom-component` for HA test fixtures
- Mock the NTA HTTP endpoints with `aioresponses` or `pytest-aiohttp`
- Mock static GTFS download to return a minimal synthetic zip
- Unit test `StaticGtfsCache` with a synthetic GTFS zip in `tests/fixtures/`
- Unit test departure merging logic in isolation (pure Python, no HA fixtures)
- Integration test via `async_setup_entry` end-to-end with mocked HTTP
- Verify all 26 acceptance criteria (plus 7a) as named test functions

## Task Sequencing (dependency order)

1. Repo scaffold: `pyproject.toml`, `manifest.json`, empty module files, `.gitignore`, `CLAUDE.md`
2. `const.py` — all constants defined
3. `static_gtfs.py` — download, parse, cache (unit-testable in isolation)
4. `coordinator.py` — NTA API fetch + parsing (unit-testable with mocked HTTP)
5. `sensor.py` — entity + departure merging logic
6. `config_flow.py` — step 1 + step 2 + re-auth
7. `__init__.py` — entry setup/teardown wiring
8. Tests — one task per logical group (entity, coordinator, config flow, static GTFS, error handling)
9. `CLAUDE.md` update with confirmed run commands once pyproject.toml is locked
