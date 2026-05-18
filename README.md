# TFI Live

[![CI](https://github.com/nocalla/tfi_live_ha/actions/workflows/ci.yml/badge.svg)](https://github.com/nocalla/tfi_live_ha/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/nocalla/tfi_live_ha)](https://github.com/nocalla/tfi_live_ha/releases)
[![License](https://img.shields.io/github/license/nocalla/tfi_live_ha)](https://github.com/nocalla/tfi_live_ha/blob/master/LICENSE)
[![Coverage](https://img.shields.io/badge/coverage-96%25-brightgreen)](https://github.com/nocalla/tfi_live_ha/actions/workflows/ci.yml)
[![HACS: Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)
[![HA: 2024.1+](https://img.shields.io/badge/Home%20Assistant-2024.1%2B-41bdf5.svg)](https://www.home-assistant.io)
[![Python: 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)](https://github.com/nocalla/tfi_live_ha/blob/master/pyproject.toml)

A Home Assistant custom integration for real-time Irish public transport departure information, powered by the National Transport Authority (NTA) GTFS-RT feed.

Each configured sensor reports the minutes to the next departure for a given stop and route, with up to three upcoming departures available as attributes. The sensor state is an integer suitable for use in automations (e.g. "leave when the next bus is 8 minutes away").

## Features

- Minutes to next departure as sensor state (integer, truncated toward zero)
- Up to 3 upcoming departures as attributes, each with scheduled time, real-time time, delay in minutes, trip ID, and route name
- Real-time data enriched with scheduled times from the static GTFS timetable
- Configurable per stop/route/direction/operator
- Automatic daily refresh of static timetable data
- Re-authentication flow when the API key is rejected

## Prerequisites

- Home Assistant 2024.1 or later
- An NTA API key — register at the [NTA Developer Portal](https://developer.nationaltransport.ie/)

## Installation

### HACS (recommended)

1. Add this repository as a custom repository in HACS (category: Integration).
2. Search for **TFI Live** and install.
3. Restart Home Assistant.

### Manual

1. Copy `custom_components/tfi_live/` into your HA `config/custom_components/` directory.
2. Restart Home Assistant.

## Configuration

Navigate to **Settings → Devices & Services → Add Integration** and search for **TFI Live**.

**Step 1 — Integration settings**

| Field | Default | Description |
|---|---|---|
| API Key | — | Your NTA API key |
| Trip Updates URL | NTA GTFS-RT endpoint | Leave as default unless using a mirror |
| Static GTFS URL | NTA static GTFS endpoint | Leave as default unless using a mirror |

**Step 2 — Add a sensor**

| Field | Required | Description |
|---|---|---|
| Name | Yes | Display name for the sensor entity |
| Stop ID | Yes | GTFS stop ID (e.g. `8220DB000836`) |
| Route ID | Yes | Route short name (e.g. `46A`, `DART`) |
| Direction ID | No | `0` or `1` — filter by direction |
| Operator ID | No | GTFS agency ID — filter by operator |

After adding a sensor you can add another or finish. Additional sensors can be added later by re-entering the config flow.

Stop and route IDs can be found in the [NTA GTFS static data](https://www.transportforireland.ie/transitData/PT_Data.html) or via tools such as [Transitland](https://www.transit.land/).

## Removal

To remove the integration:

1. Go to **Settings -> Devices & Services -> TFI Live** and select **Delete**.
2. Confirm the deletion. All associated entities are removed immediately.
3. Re-adding the integration later will restore your sensors from the configuration you enter during setup.

For **manual installs**: delete the `custom_components/tfi_live/` directory from your HA config directory and restart Home Assistant.
## Entity Model

**State:** Integer minutes to the next departure (truncated toward zero). `None` when no upcoming service is found or when the feed has not been updated within the last 3 minutes.

**Attributes:**

| Attribute | Type | Description |
|---|---|---|
| `stop_id` | string | Configured GTFS stop ID |
| `route_id` | string | Configured route short name |
| `direction_id` | int \| null | Configured direction filter |
| `operator_id` | string \| null | Configured operator filter |
| `last_updated` | ISO 8601 string | Timestamp of the last successful feed fetch |
| `departures` | list | Up to 3 upcoming departures (see below) |

Each entry in `departures`:

| Key | Type | Description |
|---|---|---|
| `scheduled_time` | `HH:MM` \| null | Scheduled departure time from timetable |
| `realtime_time` | `HH:MM` \| null | Real-time departure time from GTFS-RT feed |
| `delay_minutes` | int \| null | Delay in minutes (positive = late) |
| `trip_id` | string | GTFS trip ID |
| `route_name` | string \| null | Route short name |

## Example Automation

Trigger a notification when it is time to leave for the bus:

```yaml
automation:
  - alias: "Time to leave for the 46A"
    trigger:
      - platform: numeric_state
        entity_id: sensor.next_46a_ranelagh
        below: 8
    action:
      - service: notify.mobile_app
        data:
          message: "Next 46A in {{ states('sensor.next_46a_ranelagh') }} minutes"
```

## Data Updates

- **Real-time feed:** polled every 60 seconds from the NTA GTFS-RT endpoint.
- **Static schedule:** downloaded at startup and refreshed every 24 hours. Route names and scheduled times are sourced from this data.
- **Availability:** a sensor goes unavailable if no successful feed fetch has occurred within the past 3 minutes. It recovers automatically when the feed becomes reachable again.

## Known Limitations

- **Operator ID filter:** applies to the static schedule only. The GTFS-RT feed does not include agency information in trip updates, so the operator filter cannot be applied to real-time data.
- **Post-midnight trips:** GTFS departure times can exceed 23:59:59 (e.g. `25:30:00` for 01:30 the next day). These are wrapped to clock time and may display unexpectedly for overnight services.
- **Feed coverage:** only operators present in the NTA GTFS feed are supported. Private operators not in the feed will not appear regardless of configuration.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Sensor shows *unavailable* | Feed unreachable or API key rejected | Check the HA log for HTTP error codes; verify the API key at [developer.nationaltransport.ie](https://developer.nationaltransport.ie) |
| No departures shown | Incorrect stop/route/direction | Cross-reference stop_id and route_id against the [NTA GTFS static data](https://www.transportforireland.ie/transitData/Data/GTFS_Realtime.zip) |
| Re-authentication prompt | API key rejected (HTTP 401) | Go to **Settings → Integrations → TFI Live → Re-authenticate** and enter a valid key |

## Development

```bash
# Clone and create venv
git clone https://github.com/nocalla/tfi_live_ha.git
cd tfi_live_ha
uv sync --extra dev

# Run tests
uv run pytest

# Lint
uv run ruff check .
uv run ruff format --check .
```

The test suite uses `unittest.mock` throughout — no live network calls are made and no Home Assistant instance is required.
