# Changelog

All notable changes to PlaceRoot are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
semver as it applies to an MCP server: removing a tool or changing a
response shape is breaking, adding a tool is minor, loosening a filter or
fixing behavior is patch.

## [0.9.7] — 2026-08-15

### Fixed
- A polluted `sys.path` can no longer hijack PlaceRoot: the package is a
  regular package that resolves to exactly one directory, every data
  reader uses the traversal form instead of importing `placeroot.data` as
  a module, and `serverInfo` reports its version instead of an empty
  string. The field report was a Claude Desktop install where
  `find_places` failed with `No module named 'placeroot.data'` while
  every sibling tool worked (#293, #297).

### Added
- A privacy policy at placeroot.dev/privacy — no accounts, no telemetry,
  queries go straight from the machine to Overture's public S3 bucket,
  and the tile cache stays on disk. The policy ships in the README and
  the `.mcpb` manifest, the last gate the Claude connector directory
  requires of a local connector (#296).
- The motivation page is live at placeroot.dev/why-placeroot: what
  agents get wrong about maps, the two camps of existing tooling, and
  the evidence from the 148-question corpus (#294).

## [0.9.6] — 2026-08-14

### Added
- Official MCP registry listing: the ownership markers the registry
  validates (`mcpName` in the npm package, an `mcp-name` stamp in the
  PyPI description), and publishing it triggers the first automated
  listing at registry.modelcontextprotocol.io — every future release
  re-publishes the listing via GitHub OIDC with no stored credential
  (#289, #292).
- Pin bumps are mechanical: `scripts/bump_pin.py NEW_RELEASE` rewrites
  the pinned fallback, and `docs/PIN.md` maps everything else that moves
  with it — which artifacts regenerate, which historical measurement
  comments must not be touched. The weekly canary's stale-pin report
  points straight at the procedure (#290).
- Mirrors can check themselves: `mirror_theme.py --check-current`
  reports "mirror holds X, upstream is at Y", `--prune-releases` clears
  superseded releases (dry-run by default), and `docs/MIRROR.md` gains
  cron/Actions refresh recipes (#291).

### Fixed
- Error handlers stopped swallowing details they already had (#285).
- `search_categories` clamps its limit like every sibling instead of
  erroring (#286).

## [0.9.5] — 2026-08-14

### Fixed
- Ambiguous city names answered cleanly: 24 fresh phrasings — misspellings,
  filler words, partial names — each run cold in its own process. Five
  failures, four root causes, each fixed with a regression test. **24/24
  correct after** (was 19/24), every tool call under the 10-second cold
  target (#274, #276).
- "notre dame paris" no longer lands in Indiana: a longer name match now
  only wins between comparably prominent places — a small CDP can't
  outrank a world city.
- "harvard square cambridge" survives the third Cambridge: ambiguous
  city names contribute their namesakes as anchor alternates, searched
  in one UNION ALL statement that keeps each city's files prunable
  (8.9 s, right answer — versus 73.8 s for the OR-ed first draft).
- "plaza mayor madrid" picks the one in Madrid: `resolve_place`'s merged
  ranking gained a distance term when no division matches the whole
  query.
- "hoover dam" stops matching Adam Cox: the skip-redundant-scans gate
  now counts only geocode hits near the reference, and per-token
  searches skip feature nouns and the reference's own city word.

### Changed
- First release cut from the public repository: npm publishes behind the
  new `npm` environment gate and ships with `--provenance` again (#275).

## [0.9.4] — 2026-08-13

### Added
- Slow calls explain themselves: every scan is recorded with whether it
  was *bounded* (a bbox or id to prune on), `PLACEROOT_TRACE=1` logs the
  per-phase breakdown, and a response slower than `PLACEROOT_TRACE_SLOW_S`
  (default 10s) carries it as `timing` — the agent that waited gets the
  why with the answer. Offline invariant tests assert the shape of each
  tool's data access (a located query never scans unbounded), catching the
  bug *class* that latency sampling only ever caught one instance of.
- `resolve_place` takes `city`: the agent states the location it already
  knows instead of the server guessing it from map data alone. A hint
  bounds the search and never becomes the answer. Unresolvable-for-want-
  of-location replies carry `need: "location"` plus a `retry_with` sketch,
  and the server instructions state the division of labour.

### Fixed
- A dropped middle word no longer loses the place: "BASIS Silicon Valley
  Lower School" now finds BASIS *Independent* Silicon Valley Lower School
  (every-token matching in the places scan; 26.7s + empty → 5.5s + the
  right school). "school", "academy", "gym" and friends joined the
  feature-noun list, killing a 15.2s divisions scan that could only come
  back empty.
- "san jose airport" no longer anchors in Henan, nor "palo alto caltrain"
  in Leyte: leading word *pairs* are anchor candidates, name-prefix words
  (san, los, santa, new…) are refused as standalone anchors, and matching
  more of the query outranks a bigger population.

## [0.9.3] — 2026-08-13

### Added
- Release selection is artifact-aware: a newer Overture release is
  reported, not adopted, while the wheel's bundled artifacts (manifests,
  geocode index, land-cover grid) still match a current release — past
  `PLACEROOT_STALE_RELEASE_DAYS` freshness wins, loudly. `data_version`
  reports `artifacts: matched | unmatched`. Without this, every
  acceleration silently turned itself off on Overture's next monthly
  drop.
- The 137-query real-user-question corpus lives in `benchmarks/` with a
  cold runner and a weekly workflow gated on *correctness* — it exists
  because a per-tool timing matrix scored "Casablanca → Chile in 0.2s"
  as the best result in the suite.
- The banner is generated (`scripts/build_og_image.py`), wider (2.37:1),
  edge-to-edge, with the Vibe Mapper byline.

## [0.9.2] — 2026-08-13

### Added
- Every tool cold in **under 10 seconds** (was under 30): wheel-bundled
  stage-0 geocode index (+alternate names), a 0.1° land-cover grid built
  from Overture's own low-zoom cartography band, pin-first release
  resolution, manifest pruning everywhere, parallel + speculative
  `gers_lookup` enrichment, batched city-extent lookups.

### Fixed
- No `ORDER BY` on geometry-computed sort keys: a TopN whose key is
  `ST_Area(geometry)` defeats parquet late materialization (18.5s vs
  1.6s measured). Containment candidates rank client-side.
- Alternate-name matches keep their tier: "Casablanca" resolves to
  Morocco, not Chile; Cairo, Kyoto, Moscow, Vienna, Prague, Copenhagen
  and Beijing all settle correctly.
- Feature nouns ("Tower", "Center") can no longer anchor a query to the
  wrong hemisphere; cities outrank namesake states; anchored results
  break ties by distance ("Millennium Park Chicago" stopped answering
  from a suburb 31 km away).
- "221B Baker Street, London": 116.3s → 3.7s (one batched extent scan
  instead of one per candidate London).

## [0.9.1] — 2026-08-11

### Fixed
- `geocode_address` folds ordinal street numbers both ways — "350 5th
  Ave, New York" (and "350 Fifth Ave") now reaches NYC's "5 AVENUE"
  rows, "1 Street" reaches "1st St", with correct 11th/21st/103rd
  suffixes. A lone ordinal token never widens to a bare-digit prefix
  match, and the variant cap rises to 24 so four-token streets keep
  their abbreviated "W ... ST NW" branch.

## [0.9.0] — 2026-08-11

### Added
- Every tool answers a truly cold query — fresh install, uncached area,
  dense city — in **under 30 seconds**, most in single digits (worst case
  was 181s). The levers, each measured: wheel-bundled per-release
  file-extent manifests (108KB) so a bbox-bounded scan reads only the
  files its box intersects instead of paying a parquet-footer read per
  file per theme; finer tiles with first-touch synchronous
  materialization for the geometry-heavy themes (buildings 0.0625°,
  transportation 0.125°); one shared DuckDB metadata cache across every
  connection (cursors of one instance); DuckDB threads raised to 96 for
  the IO-bound footer passes (`PLACEROOT_DUCKDB_THREADS`); a staged
  divisions name-index build that defers admin chains to a background
  upgrade; and a startup metadata pre-warm covering every queried theme.
  `scripts/build_release_manifest.py` regenerates manifests when a new
  Overture release is pinned.
- Cold queries narrate themselves: when the client sends an MCP
  `progressToken`, slow phases — the direct S3 scan of a first query over
  a new area, each tile fetched by a synchronous cache warm — stream as
  `notifications/progress` messages instead of a silent spinner. Clients
  that don't ask for progress see no change.
- `PLACEROOT_CACHE_FETCH_CONCURRENCY` (default 2): background tile
  fetches are now bounded. Unbounded, a first query over a new area
  spawned one COPY per touched (theme, tile) — six at once with the
  recreation layer — and the answering scan starved for minutes behind
  its own cache warmers; measured first-answer latency over a two-tile
  NYC box went from 20+ minutes to well under a minute with the bound.
- Recreation layer, **on by default**: the places tools additionally read
  Overture's OSM-derived `base` theme (`type=land_use`, `type=land`) for
  the recreation areas that listings-derived data under-counts —
  playgrounds, parks, dog parks, nature reserves, beaches. ~2.5x the
  playground coverage (1,552 vs 674 across New York City), live, with no
  build step, no download and no hosting: it is one more scan of the
  release PlaceRoot already queries. Costs a second dataset scan per
  places query (tile-cached like every other read) and widens the failure
  surface to the base theme; set `PLACEROOT_RECREATION_LAYER=0` to
  restore the previous single-theme behavior exactly. A deployment that
  pins places to its own dataset (`PLACEROOT_DATA_PATH`, a mirror) without
  pinning `theme=base` skips the layer rather than reaching past that
  configuration to live S3 — set `PLACEROOT_DATA_PATH_BASE` to include it. Rows carry real
  GERS ids but no confidence or operating_status, and unnamed ones are
  returned with `name: null` rather than dropped. A place present in both
  themes is returned once (same category within 150 m — the richer places
  row wins), `place_details` labels layer rows `source_theme: base` and
  `gers_lookup` reports their real owning theme/type, an unreadable base
  dataset (e.g. a places-only mirror) degrades that branch instead of
  failing every places query, and unbounded lookups (`place_details` with
  no location hint, name-only geocode fallback) serve the layer from
  cached tiles or a pinned dataset rather than an unprunable live scan.
  `data_version` reports every degraded or skipped base type.
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

### Fixed
- `resolve_place` no longer loses to plain `geocode` on place queries:
  geocode's place-type hits merge into its candidates, the trailing-token
  anchor ranks namesake cities by prominence and searches alternate
  names ("Shibuya Crossing Tokyo" used to anchor on a Papua New Guinea
  division named Tokyo and return nothing), and place hits carry their
  Overture category
- `gers_lookup` reports a recreation-layer row's real owning theme/type;
  `place_details` labels such rows `source_theme: base`

### Changed
- README redesigned as a visual intro page: logo/OG banner header, nav
  links, grouped tool-family overview, collapsible per-client install
  configs; the full tool catalog, prompts/resources, and
  `PLACEROOT_TOOLS` details moved to `docs/REFERENCE.md`.
  `scripts/sync_npm_readme.py` now inherits the new sections and
  rewrites repo-relative links to absolute GitHub URLs for npmjs.com;
  the PyPI long description gets the same rewrite at build time via
  `hatch-fancy-pypi-readme`, so links and the banner image resolve on
  pypi.org too
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
