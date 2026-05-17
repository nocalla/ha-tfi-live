# tfi_live — Constitution

Non-negotiable principles for this project. Updated only by deliberate decision.

## Purpose

A Home Assistant custom integration that surfaces real-time NTA/TFI public transport departure data as HA sensor entities, suitable for dashboard display and automation triggers.

## Architecture Principles

1. **Config flow only.** All configuration is done via the HA UI (Settings > Integrations). No `platform:` YAML support. This is a requirement for HACS submission and modern HA integration standards.

2. **DataUpdateCoordinator pattern.** All polling is handled by HA's `DataUpdateCoordinator`. No manual `async_track_time_interval` or direct polling in entities. This ensures HA controls the update lifecycle and provides correct error handling, retry, and availability propagation.

3. **Stop-by-ID, not by name.** All stop and route lookups use IDs from the static GTFS data. Name-based matching is fragile and will not be supported.

4. **Modular source layout.** Source is split across focused files — no monolithic `sensor.py`. Minimum split: `coordinator.py` (data fetching + update logic), `sensor.py` (entity definitions), `config_flow.py` (UI setup), `static_gtfs.py` (static schedule handling), `const.py` (constants), `manifest.json`.

5. **Static GTFS is a cache, not a database.** Static schedule data is downloaded from the NTA endpoint at startup and refreshed daily. It is held in memory (pandas DataFrames) and never written to disk as a persistent database. If the cache is stale or missing, sensors degrade gracefully (real-time data only, no scheduled times in attributes).

6. **No bundled HA mock modules.** Tests use `pytest-homeassistant-custom-component` or equivalent HA test fixtures — not hand-rolled mock modules in the source tree.

## Technology Constraints

- Python 3.12+ (minimum version for current HA core)
- `uv` for environment and package management — no `pip`, no `poetry`
- `ruff` for linting and formatting (88-char line length)
- `bandit` for security scanning
- `pytest` + `pytest-cov` for testing — target 100% coverage
- Google-style docstrings on all functions (enforced by `pydocstyle`)
- Type hints on every function signature
- `hatchling` as build backend

## HA Integration Standards

- Must be compatible with the current HA core release
- Must pass HACS default checks if/when published
- `manifest.json` must specify all Python dependencies in `requirements`
- Integration must handle coordinator update failures without crashing HA
- Entities must correctly implement `available` based on coordinator state

## Versioning

Semantic versioning: `MAJOR.MINOR.PATCH`. Start at `0.1.0`. Increment MINOR for new features, PATCH for bug fixes. Do not use date-based versioning.

## Scope Boundaries

In scope: bus departures from NTA GTFS-RT trip update feed; optional vehicle position tracking; config flow; static GTFS schedule as fallback/enrichment.

Out of scope (for v1): Luas (separate API), Irish Rail via NTA (not yet covered by GTFS-RT), push notifications, persistent storage of historical departure data.
