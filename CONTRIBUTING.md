# Contributing to PlaceRoot

Thanks for your interest. This page covers how to get a working dev setup,
what the tests expect, and the design rules a change needs to fit.

## Dev setup

```bash
uv sync              # install runtime + dev dependencies
uv run pytest        # offline test suite (see below for the one-time extension setup)
uv run ruff check .  # lint
```

Python ≥ 3.11. CI runs the suite on 3.11, 3.12 and 3.13.

### Offline vs. live tests

The default `pytest` run queries small GeoParquet fixtures under
`tests/fixtures/`, built by `scripts/build_fixture.py` and friends, and
never touches the Overture release. It does need the DuckDB `httpfs` and
`spatial` extensions to be present locally: the first run on a fresh
machine downloads them once (a few MB from extensions.duckdb.org, into
`~/.duckdb/extensions/`), after which the suite is fully offline. The
runtime only runs `INSTALL` when a plain `LOAD` fails, so an extension
that is already present is loaded without any network access. To
pre-install them on a host that has no network access at test time (or
to point DuckDB at a directory you populated elsewhere, set
`PLACEROOT_DUCKDB_EXTENSION_DIR`):

```bash
uv run python -c "import duckdb; duckdb.connect().execute('INSTALL httpfs; INSTALL spatial')"
```

Tests that hit the real Overture S3 release are marked `live` and
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

Bumping `release.PINNED_RELEASE` (the fallback Overture release, watched by
the weekly canary) is its own multi-step runbook, not a single sync guard —
see [docs/PIN.md](docs/PIN.md) and `scripts/bump_pin.py`.

## Where things live

- `src/placeroot/server.py` keeps the tool handlers, the registry and
  `build_server()`. The helpers that used to sit beside them are in the
  `_server_*` modules: `_server_errors`, `_server_refs` (LocationRef
  resolution), `_server_middleware`, `_server_confirm`, `_server_schemas`
  (from-keyword schema patches) and `_server_cli`. Import the public names from
  `placeroot.server`; the underscore modules are internal.
- `src/placeroot/geocode/` is a package with an underscore submodule per
  concern. Its `__init__` docstring maps each submodule to what it holds, and
  the design notes (why the divisions table is materialised, ranking rules) are
  in [docs/GEOCODE-INTERNALS.md](docs/GEOCODE-INTERNALS.md). Import
  `placeroot.geocode` rather than a submodule, and see the docstring's note on
  `monkeypatch` before patching a helper in a test.

Ruff format is not yet enforced in CI: CI runs `ruff check` only. A
`ruff format` sweep of the tree is planned, so do not reformat unrelated code
in a PR.

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
   limit. A second read of the *same* keyless open data is compatible with
   this — see the recreation layer (`docs/RECREATION.md`), which adds no
   key, no account and no third-party service, only another scan of the
   Overture release already on the critical path.
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
appropriate `PLACEROOT_TOOLS` profiles, a row in the tool catalog in
[docs/REFERENCE.md](docs/REFERENCE.md#all-48-tools) and the site tool grid
in `site/index.html`, and offline tests against fixtures. `tests/test_site_tools_sync.py`
only guards the site side — it asserts every registered tool is shown on
the marketing site's tool grid (and vice versa) and that the site's
headline tool-count copy matches; it does not check `docs/REFERENCE.md`,
so a new tool's catalog row is on you to add and keep accurate.

## Commits and pull requests

- Keep PRs focused; one change per PR.
- CI must be green: ruff, the offline suite on all three Python versions
  (Ubuntu; macOS and Windows run the suite on 3.12), and the browser smoke job.
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
