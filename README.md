# TFI Live

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
