# Contributing to PlaceRoot

Thanks for your interest. This page covers how to get a working dev setup,
what the tests expect, and the design rules a change needs to fit.

## Dev setup

```bash
uv sync              # install runtime + dev dependencies
uv run pytest        # offline test suite (no network needed)
uv run ruff check .  # lint
```

Python ≥ 3.11. CI runs the suite on 3.11, 3.12 and 3.13.

### Offline vs. live tests

The default `pytest` run is fully offline: it queries small GeoParquet
fixtures under `tests/fixtures/`, built by `scripts/build_fixture.py` and
friends. Tests that hit the real Overture S3 release are marked `live` and
excluded by default:

```bash
uv run pytest -m live   # network required; slower
```

If your change needs new fixture data, extend the relevant
`scripts/build_*_fixture.py` script rather than hand-editing parquet files,
and say so in the PR.

### Sync guards you will meet

Several tests fail when generated or mirrored content drifts. If one fails
on your PR, run the named script rather than editing the generated file:

- `tests/test_npm_readme_sync.py` — `npm/README.md` is generated from the
  root `README.md`. After editing the root README, run
  `uv run python scripts/sync_npm_readme.py`.
- `tests/test_site_tools_sync.py` / `tests/test_site_version_sync.py` — the
  website's tool grid and version strings must match what `server.py`
  registers and `pyproject.toml` declares.
- `tests/test_registry_manifests.py` / `tests/test_mcpb_bundle.py` — the
  registry manifests (`server.json`, `mcpb/manifest.json`) are kept in step
  with the package version.

## Design rules

These are the constraints that shape every tool; a PR that fights them will
be asked to change direction, however good the code is.

1. **Answers, not data.** Every tool response fits in ~2K tokens. Large
   results return a summary plus a retrievable artifact — never a raw
   GeoJSON dump.
2. **Schema tokens are a budget.** All tool schemas together cost roughly
   13K tokens of every conversation's context, before the agent asks
   anything. **A new tool must account for its schema cost**: keep argument
   lists small, descriptions tight, and consider whether the capability
   belongs in an existing tool's arguments, a `*_batch` sibling, or a
   `PLACEROOT_TOOLS` profile instead of a new top-level tool. This is the
   most common reason a first PR needs rework.
3. **Open data only, keyless by default.** Overture Maps GeoParquet and
   services that permit anonymous use. Nothing on the critical path may
   depend on an API key, another project's roadmap, or someone else's rate
   limit. Opt-in local data an operator builds themselves is compatible with
   this — see the supplemental places layer (`docs/SUPPLEMENT.md`), which
   changes nothing on the critical path.
4. **Honest responses.** If a result is clamped, degraded, truncated, or
   outside data coverage, the response says so explicitly rather than
   returning a silently partial answer.

Permanently out of scope: **hazard- and property-risk scoring.** PlaceRoot
answers "what is where" questions. It will not ship flood/fire/wind risk
scores, property risk ratings, or insurance-flavored analytics. For what is
planned next, see the open issues.

## Proposing a new tool

Open a tool-request issue before writing code. Include: the question the
tool answers, why existing tools can't answer it, which Overture theme(s)
it reads, and a sketch of the response shape with a token estimate. Tools
also need: MCP annotations (`readOnlyHint`, title), registration in the
appropriate `PLACEROOT_TOOLS` profiles, a row in the README tool table and
the site tool grid (the sync tests will remind you), and offline tests
against fixtures.

## Commits and pull requests

- Keep PRs focused; one change per PR.
- CI must be green: ruff, the offline suite on all three Python versions,
  and the browser smoke job.
- Reference the issue you're addressing (`#123`) in the PR description.
- Response-shape changes are breaking for agents that parse our output —
  call them out explicitly in the PR description.

## Licensing of contributions

PlaceRoot is MIT-licensed. By submitting a contribution you agree that it
is licensed under the [MIT License](LICENSE) (inbound = outbound). There is
no CLA.

## Questions

Open a Discussion rather than an issue if you're not sure something is a
bug. Security reports: see [SECURITY.md](SECURITY.md) — please don't open
public issues for those.
