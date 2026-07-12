# HA TFI Live

[![CI](https://github.com/nocalla/ha-tfi-live/actions/workflows/ci.yml/badge.svg)](https://github.com/nocalla/ha-tfi-live/actions/workflows/ci.yml)
[![Release](https://img.shields.io/badge/release-v0.2.6-blue)](https://github.com/nocalla/ha-tfi-live/releases)
[![License](https://img.shields.io/badge/license-MIT-green)](https://github.com/nocalla/ha-tfi-live/blob/main/LICENSE)
[![Coverage](https://img.shields.io/badge/coverage-99%25-brightgreen)](https://github.com/nocalla/ha-tfi-live/actions/workflows/ci.yml)
[![HACS: Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)
[![HA: 2024.1+](https://img.shields.io/badge/Home%20Assistant-2024.1%2B-41bdf5.svg)](https://www.home-assistant.io)
[![Python: 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)](https://github.com/nocalla/ha-tfi-live/blob/main/pyproject.toml)

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

1. Copy `custom_components/ha_tfi_live/` into your HA `config/custom_components/` directory.
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

For **manual installs**: delete the `custom_components/ha_tfi_live/` directory from your HA config directory and restart Home Assistant.
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

- **Real-time feed:** polled every 60 seconds from the NTA GTFS-RT endpoint — the maximum rate permitted by the NTA fair usage policy (see below).
- **Static schedule:** downloaded in the background after startup and refreshed every 24 hours. Route names and scheduled times are sourced from this data, so they may be missing for the first few minutes after setup or a restart while the ~80 MB archive downloads and parses.
- **Availability:** a sensor goes unavailable if no successful feed fetch has occurred within the past 3 minutes. It recovers automatically when the feed becomes reachable again.

## Data Licence, Attribution and Fair Usage

The GTFS data shown by this integration is provided by the [National Transport Authority (NTA)](https://www.nationaltransport.ie/) under the [Creative Commons Attribution 4.0 International](https://creativecommons.org/licenses/by/4.0/) licence, subject to the [NTA GTFS fair usage policy](https://developer.nationaltransport.ie/usagepolicy). This integration's MIT licence covers the code only, not the data. The data is provided "as is" and the NTA is not responsible for any errors or inaccuracies in it.

If you display this integration's data in a public-facing application, presentation, or publication, the policy requires you to credit the NTA as the data provider, link to the GTFS data source or the [NTA website](https://www.nationaltransport.ie/), and include the "as is" statement above.

The policy limits each API token to **one GTFS-RT request every 60 seconds**. The integration polls exactly once per 60 seconds, so a single Home Assistant instance stays within the limit — but if the same API token is shared with other applications (or a second HA instance), the combined request rate will exceed it. Use a dedicated token for this integration. The static GTFS archive is refreshed once every 24 hours, well within fair usage.

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
# Clone and install
git clone https://github.com/nocalla/ha-tfi-live.git
cd ha-tfi-live
uv sync --extra dev   # installs nta-gtfs from PyPI automatically

# Run tests
uv run pytest

# Lint
uv run ruff check custom_components/ tests/
uv run ruff format --check custom_components/ tests/
```

The test suite uses `unittest.mock` throughout — no live network calls are made and no Home Assistant instance is required.

See [CONTRIBUTING.md](CONTRIBUTING.md) for full contribution guidelines and the maintainer release process.

### Library dependency

Real-time and static GTFS fetch/parse logic lives in a separate library: [`nta-gtfs`](https://pypi.org/project/nta-gtfs/) ([source](https://github.com/nocalla/python-nta-gtfs), import name `nta_gtfs`). It is published on PyPI, declared as a versioned dependency in `pyproject.toml`, and listed as a requirement in `manifest.json` so Home Assistant installs it automatically.
