<!-- Thanks for contributing! See CONTRIBUTING.md for full guidelines. -->

## What does this PR do?

<!-- Describe what changed and why. Link any related issue, e.g. "Fixes #123". -->

## Checklist

Per [CONTRIBUTING.md](../CONTRIBUTING.md):

- [ ] The PR is focused on a single change, branched from `main`
- [ ] `uv run pytest` passes locally (coverage stays at or above 95%)
- [ ] `uv run ruff check custom_components/ tests/` passes
- [ ] `uv run ruff format --check custom_components/ tests/` passes
- [ ] Tests added or updated for the change
- [ ] Commits follow [Conventional Commits](https://www.conventionalcommits.org/)
- [ ] No GTFS fetch/parse logic added here — that belongs in [`nta-gtfs`](https://github.com/nocalla/python-nta-gtfs)
