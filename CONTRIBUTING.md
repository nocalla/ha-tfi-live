# Contributing to HA TFI Live

Thanks for your interest in contributing! This document covers how to set up a
development environment, the project's conventions, and how releases are cut.

HA TFI Live is a [HACS](https://hacs.xyz)-compatible Home Assistant custom
integration (domain `tfi_live`) that surfaces real-time Irish public
transport departures from the NTA GTFS-RT feed.

## Development setup

Requirements: Python 3.12+ and [`uv`](https://docs.astral.sh/uv/). The project
uses `uv` exclusively — never pip or poetry.

```bash
git clone https://github.com/nocalla/ha-tfi-live.git
cd ha-tfi-live
uv sync --extra dev   # installs nta-gtfs from PyPI automatically
```

## Running tests and lint

```bash
# Tests (coverage gate: 95%, configured in pyproject.toml)
uv run pytest

# Lint and format check
uv run ruff check custom_components/ tests/
uv run ruff format --check custom_components/ tests/
```

Run all of the above before opening a PR. The test suite uses `unittest.mock`
throughout — no live network calls are made and no Home Assistant instance is
required.

## Code conventions

- **Google-style docstrings** on all functions, public and private.
- **Type hints** on every function signature, no exceptions. CI runs `mypy`
  in strict mode against `custom_components/tfi_live`.
- **Config flow only** — no YAML `platform:` setup support.
- **GTFS fetch/parse logic lives in the library, not here.** All real-time and
  static GTFS fetching and parsing belongs in the separate
  [`nta-gtfs`](https://pypi.org/project/nta-gtfs/) library
  ([source](https://github.com/nocalla/python-nta-gtfs), import name
  `nta_gtfs`). PRs to this repo should not add fetch/parse logic; open a PR
  against `python-nta-gtfs` instead.
- **Integration tests mock the library at the class level** — patch
  `nta_gtfs.GtfsRtClient` and `nta_gtfs.StaticGtfsClient`, not raw aiohttp
  sessions.

## Commits and pull requests

Commits follow [Conventional Commits](https://www.conventionalcommits.org/):

```
feat: add operator filter to config flow
fix: handle post-midnight GTFS departure times
docs: update README installation steps
chore: bump nta-gtfs to 0.2.0
ci: add hassfest validation workflow
```

For pull requests:

1. Branch from `main` and keep the PR focused on a single change.
2. Add or update tests — coverage must stay at or above 95%.
3. Make sure `uv run pytest` and both ruff commands pass locally.
4. Open the PR against `main` and describe what changed and why.

Two workflows run on every PR:

- **CI** (`.github/workflows/ci.yml`) — runs `ruff format` and
  `ruff check --fix` and pushes any auto-fix commits back to your branch
  (for branches in this repo), then runs `ruff check`, `mypy`, and `pytest`.
  Don't be surprised if a `style: apply ruff auto-fix` commit appears on
  your branch.
- **Validate** (`.github/workflows/validate.yml`) — runs HACS validation
  (`hacs/action`) and Home Assistant's hassfest validation.

All jobs must be green before merge.

## Maintainer guide: cutting a release

Releases are fully automated by `.github/workflows/release.yml`, and can be
triggered two ways:

1. **Automatically on merge:** label the PR `release: patch`, `release: minor`,
   or `release: major` before merging. Once the merge lands on `main`, the
   workflow looks up the label on the merged PR and cuts a release with that
   bump type. PRs without one of these labels (e.g. docs-only or CI-only
   changes) merge normally with no release. If a PR carries more than one
   `release: *` label, `major` wins over `minor` over `patch`.
2. **Manually:** run the workflow with an explicit bump type, useful for
   releasing without going through a labelled PR (e.g. after a direct push
   to `main`).
   - **GitHub UI:** Actions → Release → Run workflow → choose the bump type.
   - **CLI:** `gh workflow run Release -f bump=patch` (or `minor` / `major`).

Either way, the workflow does everything from there — no manual tagging or
release drafting is needed:

1. Runs the full test suite (the release aborts if tests fail).
2. Bumps the version (per the bump type) in:
   - `custom_components/tfi_live/manifest.json` (the source of truth for
     the current version),
   - `pyproject.toml`.
3. Commits the changes as `chore: release vX.Y.Z`, tags `vX.Y.Z`, and pushes
   both to `main`.
4. Builds `ha-tfi-live.zip` from `custom_components/tfi_live/`.
5. Creates a GitHub release for the tag with auto-generated release notes and
   the zip attached.

The Release badge in `README.md` is dynamic (sourced live from the GitHub
releases API), so it needs no update as part of this process.

HACS picks up new versions from GitHub release tags, so publishing the release
is all that is needed for users to see the update.
