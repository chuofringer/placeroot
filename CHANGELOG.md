# Changelog

All notable changes to PlaceRoot are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
semver as it applies to an MCP server: removing a tool or changing a
response shape is breaking, adding a tool is minor, loosening a filter or
fixing behavior is patch.

## [Unreleased]

### Added
- Optional recreation layer (`PLACEROOT_RECREATION_LAYER`): the places
  tools additionally read Overture's OSM-derived `base` theme
  (`type=land_use`, `type=land`) for the recreation areas that
  listings-derived data under-counts — playgrounds, parks, dog parks,
  nature reserves, beaches. ~2.5x the playground coverage (1,552 vs 674
  across New York City), live, with no build step, no download and no
  hosting: it is one more scan of the release PlaceRoot already queries.
  Opt-in because it costs a second scan per query; unset, the critical
  path is unchanged. Rows carry real GERS ids but no confidence or
  operating_status, and unnamed ones are returned rather than dropped.
  See [docs/RECREATION.md](docs/RECREATION.md)
- `geocode_address`: street-level forward search — "1600 Amphitheatre
  Parkway, Mountain View" → coordinates, city-bounded and deduplicated
  (#225, #229)
- Overture release freshness: TTL-based re-discovery
  (`PLACEROOT_RELEASE_TTL_HOURS`, default 6h) instead of process-lifetime
  caching, a staleness signal in `data_version`
  (`PLACEROOT_STALE_RELEASE_DAYS`, default 60), and a weekly upstream
  canary workflow that opens/closes its own issue (#219, #238)
- Claude Desktop Extension bundle (`.mcpb`), built by
  `scripts/build_mcpb.py` and served from the site (#233, #239)
- `geocode`: guarded fuzzy fallback tier for typos (#215, #234); searches
  Overture's alternate names (`names.common`) (#214, #220); postcode-shaped
  queries return a centroid plus covering locality (#223, #226)

### Changed
- `geocode` ranking: prominence can rescue prefix matches; diacritic
  folding always runs (#221, #222)
- Divisions table carries bbox columns for city-bounded scans (#224, #228)
- Site: prompt-first install page with a Claude Desktop Chat/Code fork
  (#233, #235)

## [0.8.0] — 2026-08-08

### Added
- Progressive disclosure mode (`PLACEROOT_TOOLS=progressive`): a 3-tool
  meta surface (`placeroot_capabilities` + `placeroot_call`) that makes all
  29 tools reachable for ~550 schema tokens (#210, #211)
- MCP 2026-07-28 listing cache hints: `ttlMs`/`cacheScope` on `tools/list`
  and the prompt/resource listings (#209, #212)
- Head-to-head token benchmark against Mapbox MCP and the archived Google
  Maps reference server, with vendored, provenance-tracked snapshots
  (#208, #213)

### Changed
- Site install UX: one-click deeplinks; registry links demoted (#227, #232)
- `geocode`: the places fallback no longer anchors on stopword fragments
  (#216, #217)

### Fixed
- Cache eviction exempts non-tile support tables; a missing-table race
  degrades instead of erroring (#230, #231)

## [0.7.0] — 2026-08-08

### Added
- `optimize_route`: multi-stop ordering (exact small-TSP) over one shared
  street graph (#177, #190)
- `water_near`: hydrology proximity from Overture's base/water theme
  (#200, #201)
- `address_at`: nearest Overture address points to a coordinate (#188, #191)
- `route`: optional simplified path geometry via `include_path` (#161, #192)
- MCP resources: `placeroot://data-version` and `placeroot://categories`
  (#195, #198)
- MCP workflow prompts: `site_selection`, `compare_neighborhoods`,
  `plan_errands` (#194, #197)
- MCP tool annotations: `readOnlyHint` + human-readable titles on every
  tool (#193, #196)

### Changed
- The addresses theme now routes through the tile cache (#189, #202)
- `npm/README.md` is generated from the root README and drift-guarded
  (#203, #205)

## [0.6.0] — 2026-08-07

### Added
- `PLACEROOT_TOOLS` subset profiles: load only the tools an install uses
  (#182, #184)
- `gers_lookup`: resolve any GERS id across themes with related joins
  (#173, #181)
- `infrastructure_at`: Overture base-theme infrastructure near a point
  (#179, #180)
- `places_along_route`: corridor search between two points (#171, #176)
- Token-efficiency benchmark: measured per-answer and schema-surface costs
  (#178, #183)
- Registry distribution artifacts: `server.json` (official MCP registry)
  and `mcpb/manifest.json` (#172, #174)

### Changed
- Releases bump the site with the package and are gated on the two agreeing
  (#170, #175)

## [0.5.0] — 2026-08-07

### Added
- `route`: shortest-path distance and duration between two points on a
  street graph built from Overture transportation segments (#160, #162)
- `land_use_at`: land use / land cover classification at a point (#167, #168)

### Fixed
- `find_places`: ILIKE wildcards escaped; `operating_status` validated
  before the schema gate (#165, #166)
- Coordinate inputs hardened: range validation and pole-query span clamp
  (#163, #164)
- Tile cache claims tiles before fetching in the sync path, closing a
  duplicate-fetch race (#158, #159)

## [0.4.1] — 2026-08-07

### Added
- Resolved division polygons persist to an on-disk cache (#152, #153, #155)

### Fixed
- Production site-deploy step (#157)

## [0.4.0] — 2026-08-07

Six new tools, richer `find_places`, and correctness fixes (#154). This
release and everything above it is fully traceable in the git log; the
0.1–0.3 summaries below predate the current history.

## [0.3.0] and earlier — 2026-08-06

- **0.3** — geocoding built on Overture's divisions/addresses themes,
  admin hierarchy lookup, area comparison, HTTP transport
- **0.2** — correctness and resilience: circular distance in SQL, honest
  counts, Overture release auto-discovery with a pinned fallback, the
  offline fixture test suite
- **0.1** — first tools: `find_places` and `summarize_area`
