---
title: "001 — Core Sensor Integration"
status: agreed
created: 2026-05-17
---

# Core Sensor Integration

## Purpose

Provide a Home Assistant custom integration (`tfi_live`) that surfaces real-time Irish public transport departure information from the NTA/TFI GTFS-RT feed as HA sensor entities, suitable for dashboard display and automation triggers.

---

## Scope

### In scope

- Config flow (UI-only) to configure the integration and add monitored stop/route sensors
- One `sensor` entity per configured stop/route combination
- Polling of the GTFS-RT trip update feed via the DataUpdateCoordinator pattern
- Download and in-memory caching of the static GTFS schedule, refreshed daily
- Graceful degradation when static GTFS data is unavailable
- Graceful degradation when the GTFS-RT feed is unreachable
- Re-authentication flow when the API key is rejected (HTTP 401)

### Out of scope

- YAML platform configuration (`platform:` entries)
- Luas or Irish Rail-specific handling
- Notification or alert entities
- Persistent storage of historical departure data
- HACS manifest or publication tooling
- Name-based stop or route lookup (IDs only)
- Vehicle position tracking / `device_tracker` entities (deferred to spec 002)

---

## Entity Model

### Sensor entity

One sensor entity is created for each stop/route combination added by the user during configuration.

**State**

- Type: integer
- Value: minutes to the next upcoming departure, rounded down (floor division)
- Negative values are valid and indicate an overdue departure (e.g. `-2` means 2 minutes past the scheduled/predicted time)
- When no real-time or scheduled departures are found for the configured stop/route (e.g. last service of the day has run), the sensor state is `None` (HA will display as "Unknown"). This is distinct from `available = False`; the sensor is available, it simply has no upcoming service to report.
- Unit of measurement: `min`
- Device class: none

**Attributes**

All of the following attributes must be present on every sensor entity at all times. When a value is unavailable, the attribute is present with the value `None` (not absent).

| Attribute | Type | Description |
|---|---|---|
| `stop_id` | `str` | The configured GTFS stop ID |
| `route_id` | `str` | The configured GTFS route ID |
| `direction_id` | `int \| None` | The configured direction filter; `None` if not set |
| `operator_id` | `str \| None` | The configured operator/agency filter; `None` if not set |
| `departures` | `list[dict]` | Next 3 upcoming departures (see below) |
| `last_updated` | `str \| None` | ISO 8601 timestamp of the last successful GTFS-RT data fetch; `None` if no successful fetch has occurred |

**Departures list**

Each entry in `departures` is a dictionary with the following keys. The list contains at most 3 entries, sorted ascending by effective departure time (real-time time if available for that trip, otherwise scheduled time). If fewer than 3 departures are found, the list contains only those found. If no departures are found, the list is empty.

| Key | Type | Description |
|---|---|---|
| `scheduled_time` | `str \| None` | Scheduled departure time in `HH:MM` format from static GTFS; `None` if static data is unavailable |
| `realtime_time` | `str \| None` | Real-time adjusted departure time in `HH:MM` format; `None` if no real-time data exists for this trip |
| `delay_minutes` | `int \| None` | Delay in whole minutes (positive = late, negative = early, `0` = on time); `None` if no real-time data exists for this trip |
| `trip_id` | `str` | GTFS trip ID |
| `route_name` | `str \| None` | Human-readable route short name from static GTFS; `None` if static data is unavailable |

**Availability**

- The sensor reports `available = True` when the coordinator has received a successful GTFS-RT data fetch within the last 3 minutes.
- The sensor reports `available = False` when no successful fetch has occurred within the last 3 minutes (e.g. feed is down, API key invalid, or network error).
- When `available = False`, the sensor state and all attributes must be cleared (returned as `None` or `unknown` per HA convention).

---

## Configuration

Configuration is performed entirely through the HA UI config flow. No YAML platform configuration is supported.

### Step 1 — Integration-level settings (set once per integration entry)

These values are shared across all sensors created under this integration entry.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `api_key` | `str` | Yes | — | NTA GTFS-RT API key |
| `trip_update_url` | `str` | Yes | `https://gtfsr.transportforireland.ie/v2/TripUpdates?format=json` | GTFS-RT trip updates feed URL |
| `static_gtfs_url` | `str` | Yes | `https://www.transportforireland.ie/transitData/Data/GTFS_Realtime.zip` | Static GTFS zip download URL |

Validation at this step:
- `api_key` must not be empty.
- `trip_update_url` must be a valid URL (parseable scheme + host).
- `static_gtfs_url` must be a valid URL (parseable scheme + host).
- The config flow must not make a live API call to validate credentials at this step (the API key is validated at runtime when the first update fires).

### Step 2 — Add a sensor (repeated per departure to monitor)

Each submission of step 2 creates one sensor entity.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `name` | `str` | Yes | — | Sensor display name (e.g. "Next 46A to City Centre") |
| `stop_id` | `str` | Yes | — | GTFS stop ID |
| `route_id` | `str` | Yes | — | GTFS route ID (short name, e.g. "46A") |
| `direction_id` | `int` | No | `None` | GTFS direction filter (0 or 1) |
| `operator_id` | `str` | No | `None` | GTFS agency ID filter |

Validation at this step:
- `name` must not be empty.
- `stop_id` must not be empty.
- `route_id` must not be empty.
- If provided, `direction_id` must be `0` or `1`.
- The config flow must not make a live API call to validate stop or route IDs at this step.

The user may return to add additional sensors after the integration is set up. Each addition goes through step 2 only; step 1 values are not re-entered.

---

## Data Sources and Update Cycle

### GTFS-RT trip updates

- The integration polls the configured `trip_update_url` on a 60-second interval.
- A single DataUpdateCoordinator manages this feed. All sensor entities share this coordinator.
- The HTTP request includes the `api_key` as an `x-api-key` header (or as specified by the NTA GTFS-RT API — the spec does not prescribe header name; implementer should consult NTA documentation).
- The response is a GTFS-RT FeedMessage in JSON-encoded protobuf format.

### Static GTFS

- At integration startup, the integration downloads the static GTFS zip from `static_gtfs_url` and loads it into memory.
- The static data is refreshed once per 24-hour period during an integration session. The refresh does not require an HA restart.
- Static data is never written to disk as a persistent database.
- If the static GTFS download fails at startup (any error), the integration starts without static data. Sensors operate in degraded mode (see Graceful Degradation).
- If the daily refresh fails, the integration continues using the last successfully loaded static data until the next refresh attempt succeeds.

---

## Graceful Degradation

### Static GTFS unavailable

When no static GTFS data has been successfully loaded:
- `scheduled_time` in each departure dict is `None`.
- `route_name` in each departure dict is `None`.
- All other sensor state and attributes derived from GTFS-RT data are unaffected.
- The integration is not marked as unavailable or failed.
- A log entry at WARNING level is emitted once per failed download attempt.

### GTFS-RT feed unavailable

When the coordinator cannot fetch GTFS-RT data and the last successful fetch was more than 3 minutes ago:
- All sensor entities under the coordinator report `available = False`.
- State and attributes are cleared.
- The coordinator is marked as failed; HA's standard coordinator retry behaviour applies.

---

## Error Handling

| Condition | Log level | Integration response |
|---|---|---|
| HTTP 4xx (except 401) from GTFS-RT feed | `WARNING` | Coordinator marked failed; HA retry applies |
| HTTP 5xx from GTFS-RT feed | `WARNING` | Coordinator marked failed; HA retry applies |
| HTTP timeout from GTFS-RT feed | `WARNING` | Coordinator marked failed; HA retry applies |
| HTTP 401 from GTFS-RT feed | `ERROR` | Config entry marked as requiring re-authentication; sensors become unavailable |
| Unparseable or invalid protobuf/JSON response | `ERROR` | Coordinator marked failed; HA retry applies |
| Static GTFS download failure | `WARNING` | Integration continues in degraded mode (no static data) |
| Static GTFS parse failure | `WARNING` | Integration continues in degraded mode (no static data) |

A single log entry per failure event is sufficient; the integration must not emit repeated identical log entries on every poll cycle for the same persistent fault.

### Re-authentication flow

When a 401 is received from the GTFS-RT feed:
- The config entry is flagged as requiring re-authentication using HA's standard `async_start_reauth` mechanism.
- The re-auth flow presents a form to re-enter the `api_key` only; all other configuration values are preserved.
- After successful re-authentication, the coordinator resumes polling and sensors become available again.

---

## Acceptance Criteria

The following criteria define "done" for this feature. Each is independently testable.

1. **Entity creation — sensor count.** Given a config entry with N stop/route sensors configured, exactly N sensor entities are created and registered in HA after setup completes.

2. **No device_tracker entities.** Zero `device_tracker` entities are created at any time in v1. Vehicle position tracking is out of scope for this spec.

3. **Sensor state — minutes calculation.** Given a GTFS-RT feed containing a departure for the configured stop/route with a real-time arrival time T minutes in the future (T may be fractional), the sensor state equals `floor(T)`. For T = 2.9, state = 2. For T = −1.3, state = −1.

4. **Sensor state — negative when overdue.** Given a departure whose real-time time has passed by 2 minutes and 45 seconds, the sensor state is `−2`.

5. **Sensor state — no real-time, uses scheduled.** Given a departure that has scheduled time data in static GTFS but no real-time entry in the GTFS-RT feed, the sensor state is calculated from the scheduled time and `realtime_time` in the departure dict is `None`.

6. **Departures attribute — correct structure.** Given a GTFS-RT feed with 5 matching departures for the configured stop/route, the `departures` attribute contains exactly 3 entries, each a dict with keys `scheduled_time`, `realtime_time`, `delay_minutes`, `trip_id`, and `route_name`. No other keys are present.

7. **Departures attribute — fewer than 3 results.** Given a GTFS-RT feed with 1 matching departure for the configured stop/route, the `departures` attribute contains exactly 1 entry.

7a. **Departures attribute — sort order.** Given 3 matching departures where departure A has no real-time data (scheduled at 09:10), departure B has real-time time 09:05, and departure C has real-time time 09:08, the `departures` list is ordered [B, C, A] — sorted ascending by effective departure time (real-time if available, scheduled otherwise).

8. **Departures attribute — no matches.** Given a GTFS-RT feed with no matching departures for the configured stop/route and no scheduled departures in static GTFS, the `departures` attribute is an empty list and the sensor state is `None` (displayed as "Unknown" in HA). The sensor remains `available = True`.

9. **Static GTFS unavailable — graceful degradation.** Given that static GTFS data failed to load, the sensor state is still populated from GTFS-RT data, `scheduled_time` is `None` for all departure entries, `route_name` is `None` for all departure entries, and the sensor reports `available = True` provided the GTFS-RT coordinator is healthy.

10. **Static GTFS unavailable — integration does not fail.** Given that the static GTFS download returns an HTTP error or a connection timeout at startup, the integration setup completes without raising an error and HA does not log the config entry as failed.

11. **GTFS-RT unavailable — availability flag.** Given that the last successful GTFS-RT fetch occurred more than 3 minutes ago (simulated by advancing time or by providing a stale last-success timestamp to the coordinator), all sensor entities under that coordinator report `available = False`.

12. **GTFS-RT unavailable — attributes cleared.** When `available = False`, the sensor state is `None` (or `unknown`) and all attributes are cleared to `None`.

13. **GTFS-RT available — availability flag.** Given a successful GTFS-RT fetch within the last 3 minutes, the sensor reports `available = True`.

14. **Coordinator update cycle — 60 seconds.** The coordinator is configured with an update interval of 60 seconds. No polling occurs at intervals shorter than 60 seconds under normal operation.

15. **HTTP error handling — WARNING log.** Given that the GTFS-RT feed returns HTTP 500, a log entry at WARNING level is emitted and the coordinator is marked failed. No exception propagates to HA core.

16. **HTTP error handling — no repeated logs.** Given that the GTFS-RT feed has been returning HTTP 500 for 5 consecutive poll cycles, the integration emits at most one WARNING log entry for the first failure, not one per cycle.

17. **401 re-auth trigger.** Given that the GTFS-RT feed returns HTTP 401, the config entry is flagged for re-authentication using HA's standard mechanism, a log entry at ERROR level is emitted, and sensors become unavailable.

18. **Re-auth flow — preserves config.** Given a re-auth flow triggered by a 401, after the user enters a new `api_key` and submits, all other configuration values (`trip_update_url`, `vehicle_position_url`, `static_gtfs_url`, all sensor stop/route settings) are unchanged.

19. **Config flow step 1 — required field validation.** Submitting step 1 of the config flow with `api_key` empty produces a validation error on that field and does not advance to step 2.

20. **Config flow step 1 — URL validation.** Submitting step 1 with a malformed (non-URL) value for `trip_update_url` or `static_gtfs_url` produces a validation error on the offending field.

21. **Config flow step 2 — required field validation.** Submitting step 2 with `name`, `stop_id`, or `route_id` empty produces a validation error on the offending field and does not create an entity.

22. **Config flow step 2 — direction_id validation.** Submitting step 2 with `direction_id` set to a value other than `0` or `1` (e.g. `2`) produces a validation error on that field.

23. **Config flow step 2 — repeated addition.** After completing step 2 and creating a first sensor, the user can return to step 2 to add a second sensor without re-entering step 1 values. The integration then has exactly 2 sensor entities.

24. **Sensor attributes — config values present.** The `stop_id`, `route_id`, `direction_id`, and `operator_id` attributes on a sensor entity exactly match the values entered in config flow step 2 for that sensor.

25. **Last updated attribute.** After a successful GTFS-RT coordinator update, `last_updated` is a valid ISO 8601 timestamp string representing a time no earlier than the start of that update call and no later than its completion.

26. **Invalid protobuf — ERROR log.** Given that the GTFS-RT feed returns a 200 response with an unparseable body, a log entry at ERROR level is emitted and the coordinator is marked failed. No exception propagates to HA core.

---

## Constraints and Notes

- All constraints in `specs/CONSTITUTION.md` apply. In particular: config flow only (no YAML platform), DataUpdateCoordinator for all polling, stop-by-ID lookups only, static GTFS held in memory only.
- The integration domain name is `tfi_live`.
- Sensor unique IDs must be stable across HA restarts. A unique ID must be derived from the integration entry ID combined with the `stop_id` and `route_id` (and `direction_id` and `operator_id` if provided) so that HA can recognise the same entity across restarts.
- Sensor unique ID construction must produce distinct IDs for two sensors that share `stop_id` and `route_id` but differ in `direction_id` or `operator_id`.
- The spec does not constrain how minutes-to-departure is calculated when both `scheduled_time` and `realtime_time` are available for the same trip. The state must reflect the real-time time when available; the scheduled time is for display in attributes only.
- Times in the `departures` attribute are local Irish time in `HH:MM` format (Europe/Dublin timezone). The spec does not prescribe how timezone conversion is performed internally.
