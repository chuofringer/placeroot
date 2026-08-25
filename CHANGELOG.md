# Changelog

All notable changes to PlaceRoot are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
semver as it applies to an MCP server: removing a tool or changing a
response shape is breaking, adding a tool is minor, loosening a filter or
fixing behavior is patch.

## [Unreleased]

### Added
- Result-language preference (#410, north-star: pelias/pelias#979 and #967;
  competitive: Nominatim's `accept-language`). A `lang` field (2-3 lowercase
  letters) on the stored `preferences()` document, plus a per-call `lang`
  override on `geocode`, `resolve_place`, and `place_details` (per-call
  wins). When Overture's `names.common` carries a language-tagged variant
  for the matched row, `name` becomes that variant and `name_primary` is
  added only when it differs from the primary — never invented or
  transliterated, and never a payload change when no variant exists or no
  `lang` is given. Divisions get a new small local `lang_names.parquet`
  table (materialized alongside #214's alt-name table, from the same
  `names.common` scan) so a lang lookup is one indexed join by the page of
  result ids, not a second scan; `place_details` piggybacks the variant
  onto its existing single-row places query as one extra column. Scoped to
  the four name-heavy answer tools this round — `find_places` rows (and,
  through it, `resolve_place`'s place-kind candidates) keep primary names,
  matching `find_places`' own documented scope line.
- `PLACEROOT_HOME=<city/area>` (#406, docs/ROADMAP.md next tier): a home
  region, resolved once lazily through the same geocode ranking and cached
  for the process, biases `geocode`/`geocode_batch`/`resolve_place` (and
  anything routing through the same ranking, e.g. `find_near`/`from_to`
  name resolution) toward it — kills the "which Springfield" class of
  ambiguity for a single-metro install. A *bias*, never a filter: a
  bounded score bonus (0.03 on `rank_score`'s ~0-1 scale, plus the matching
  tuple position in the internal sort key) only ever breaks a same-tier tie
  toward home — a genuinely better-ranked distant match still wins, and
  out-of-region results are never dropped from the answer. `geocode`'s
  reply carries a one-line disclosure note only when the bias actually
  changed the top result (computed by comparing the biased and unbiased
  winner on the same already-scored candidate list); no note otherwise. If
  the home text resolves, its tile cache is pre-warmed at startup the same
  way a city-scale resolve warms one, on a background thread that never
  blocks startup and respects the existing `PLACEROOT_CACHE=off` knob. No
  `PLACEROOT_HOME` set -> byte-identical ranking to before this change.
  MCP roots as a second config source (a client-published `placeroot.json`)
  is investigated but left a documented stub
  (`home_region.resolve_home_from_roots`): the pinned SDK
  (`mcp[cli]>=2.0.0`) only exposes server-side roots access from inside an
  in-flight request's session (`ServerSession.list_roots`), and that method
  is itself marked deprecated as of 2026-07-28 (SEP-2577) in this SDK
  version — threading an async, per-request, now-deprecated capability
  through the shared, synchronous ranking path every geocode/resolve_place
  call uses would mean contorting the architecture for a capability the SDK
  itself is walking back. `PLACEROOT_HOME` is the supported path.
- `plan_area_visit(area, interests, mode?)`, a seventh workflow prompt
  (docs/ROADMAP.md §3B, issue #405) — plans a visit around an area and a
  list of interests with the post-#398/#399 idioms: `search_categories`
  only for interest phrases that aren't already known slugs, one
  `find_places` scan with up to 5 category slugs and
  `group_by_category=true` (not one call per category), `optimize_route`
  over the chosen stops' GERS ids/names, then `render_map`. `mode`
  defaults from stored `preferences()` when omitted, walking otherwise.
  The final itinerary is compact (ordered stops, one line why each,
  total time) and surfaces trust tiers/trust_note honesty explicitly,
  since `optimize_route`'s own `verify_before_going` does not fire for
  id/name stops. Registered under every profile, like its six siblings;
  costs zero `tools/list` tokens.
- `union`/`intersect`/`difference` ops on `geometry_op` (docs/ROADMAP.md
  next-tier "Geometry set ops", #404) — Mapbox ships these as Turf tools
  and agents do use them; they were the tool's only "not implemented"
  ops. Each takes `geometry` + the new `geometry2` param (both Polygon or
  MultiPolygon) and returns `{"geometry", "area_km2"}`, computed via the
  DuckDB spatial extension already loaded for other tools
  (`ST_Union`/`ST_Intersection`/`ST_Difference` — the same machinery
  `area_suggest.intersect_sheds` uses) rather than a hand-rolled
  pure-Python clipper, since `geometry_ops.py` is deliberately
  connection-free by design. The result geometry passes through the same
  vertex-budget/simplify convention `buffer`/`convex_hull` already use — no
  unbounded coordinate dump. A genuinely empty result (a disjoint
  `intersect`, or a `difference` fully covered by `geometry2`) returns
  `{"empty": true, "note": "..."}`, not a null or zero-area geometry.
- Declared MCP `outputSchema` on all 42 tools (docs/ROADMAP.md §4 feature 3
  / §5.3) — the one MCP-conformance gap docs/benchmarks-vs.md conceded to
  Mapbox is closed. Hand-authored, not derived: every tool returns a bare,
  heterogeneous `dict` (a success shape or an `{"error", "detail", ...}`
  envelope), so the SDK's own return-type schema derivation produces
  nothing for any of them. `find_places`, `find_near`, `geocode`,
  `geocode_batch`, `resolve_place`, `from_to`, `route`, `isochrone`,
  `travel_time_matrix`, `distance_matrix`, `optimize_route`, and
  `data_version` get precise per-field schemas; every other tool gets the
  same honest generic envelope. Every schema is `{anyOf: [<success shape,
  additionalProperties: true>, <shared error envelope>]}`, so a real
  answer — success, error, or a degraded/needs_confirm variant — always
  validates; `tests/test_output_schemas.py` proves it against real calls
  over the committed offline fixtures, both server-side and through the
  real MCP client layer. Tool calls also now carry `structuredContent`
  (additive — a spec-compliant client requires it once a tool declares an
  outputSchema; the existing text `content` is unchanged), reparsed from a
  permissive pass-through model rather than validated against the
  published schema server-side, so a declared schema can never make a
  previously-working call fail.
- `try` hints on dead-end errors (docs/ROADMAP.md §4, next tier): a
  `not_found` from resolving a free-text name or a LocationRef GERS id
  (`from_to`, `find_near`, and every LocationRef-accepting tool — they all
  route through the same two internal helpers, `_resolve_named_place` and
  `_resolve_location_ref`) now carries a short machine-actionable `"try"`
  string naming the next move — e.g. `"resolve_place with near_lat/near_lon
  or city to disambiguate; or geocode for street addresses"`. `route`'s and
  `places_along_route`'s `no_route` (both points snapped, nothing connects
  them) carries a mode-tuned `"try"` — walk/cycle name the footpaths and
  pedestrian bridges drive cannot reach; walk's own hint stays a plain
  connectivity check rather than pointing at a nonexistent "more capable"
  mode. `resolve_place`'s existing `need`/`retry_with` sketch already
  covers its own no-match case, so it was left alone — the two errors serve
  the same purpose through different shapes, and adding "try" there would
  be redundant. Every other `not_found` site (a caller-supplied
  `division_id`/GERS id that doesn't exist, rather than a name that failed
  to resolve) was deliberately left unchanged — see the PR body's
  site → hint table.
- LocationRef, wave 2 (docs/ROADMAP.md §4.1): `distance_matrix`'s and
  `travel_time_matrix`'s `origins`/`destinations`, `meeting_point`'s
  `origins`, and `ground_location`'s and `within_distance`'s new `where`
  now accept the same LocationRef — `{"lat": ..., "lon": ...}`, a GERS id
  string, or a free-text place name — mixed freely inside a list, same
  conventions as wave 1: parallel resolution, indexed errors
  (`origins[i]: ...` / `destinations[i]: ...`), a compact `resolved` echo
  only for string entries, and a byte-identical answer for
  coordinate-only calls. The matrix tools' `resolved` splits into
  `{"origins": [...], "destinations": [...]}`, each present only when that
  side had a string entry. `meeting_point`'s per-origin `mode` stays a
  dict-only field (`{"lat", "lon", "mode"}`); a string origin always gets
  the default mode. `ground_location`'s and `within_distance`'s `where` is
  mutually exclusive with `lat`/`lon`, same as `isochrone`/`summarize_area`
  in wave 1. `route` gains a docstring pointer to `from_to` for name/id
  endpoints instead of its own `where` (it already has two coordinate
  pairs; `from_to` is the LocationRef-native route). `elevation_at`,
  `address_at`, `buildings_at`, `land_use_at`, `infrastructure_at`,
  `water_near`, `admin_lookup`, `changes_in_area`, `summarize_buildings`
  and `reverse_geocode` are deliberately not widened this wave — micro
  point-lookups where a geocode hop is rare and the schema cost per tool
  adds up. `within_distance`'s `max_distance_m` is now keyword-only with
  no default, so it stays a required, non-defaultable schema field even
  though `lat`/`lon` had to become optional to make room for `where` — an
  omitted `max_distance_m` is refused before the tool runs rather than
  silently searching a 0m window. `meeting_point`'s string origins now
  resolve in parallel (same pattern `_resolve_location_refs` already
  uses), not one name at a time, keeping a multi-person named-origin call
  inside the project's response-time budget.
- LocationRef, wave 1 (docs/ROADMAP.md §4.1): `optimize_route`'s `stops`,
  `compare_areas`'s `areas`, `isochrone`'s and `summarize_area`'s new
  `where`, and `from_to`'s `from`/`to` now accept a location as
  `{"lat": ..., "lon": ...}`, a GERS id string, or a free-text place name
  — mixed freely inside a list. Resolving a name/id no longer requires a
  separate `geocode`/`resolve_place` round-trip first. A string input adds
  a compact `resolved`/`from`/`to` echo (`name`, `id`, `lat`, `lon`,
  `matched_by`); a plain `{lat, lon}` input produces a byte-identical
  answer to before this change — no new keys. List parameters resolve
  their string entries in parallel and report the first failing index as
  `{"error": ..., "index": i, "detail": "stops[i]: ..."}` so the agent can
  retry that one argument; an ambiguous name still returns
  `ambiguous_place` with `candidates`, indexed the same way. `optimize_route`
  and `compare_areas` on named areas compare the same `radius_m` circle
  around the resolved point as a coordinate input would — polygon-accurate
  area comparison is a later wave. `route`, `distance_matrix`,
  `travel_time_matrix`, `meeting_point`, `ground_location` and the rest of
  the surface are out of scope for this wave; `find_near` and `from_to`
  are not deprecated.
- Stateless pagination cursors (docs/ROADMAP.md §4.4): a `find_places` or
  `find_near` answer that's truncated — either token-budget-trimmed, or
  because the scan stopped at `limit`/`MAX_ROWS` while more matching rows
  exist — now carries `cursor`, an opaque, self-contained token encoding
  `(query hash, Overture release, row offset)`. Pass it back with the same
  arguments to fetch the next page; a cursor for a different query (or a
  malformed one) returns `{"error": "bad_cursor", ...}` naming the
  mismatch, and a cursor issued against an older Overture release is
  honored anyway — served against the current release, with a one-line
  `note` that rows may have shifted, rather than failing the continuation.
  No server-side session state: correctness relies only on PlaceRoot's
  answers coming from a pinned, immutable release. `find_places`' point
  path gained a stable `id` tiebreak on its distance ordering (the division
  path already had one) so pages never overlap or skip on tied distances.
  Purely additive: `cursor` appears only on a truncated answer, and
  `truncated`/`omitted_count` keep their existing meaning for the
  budget-trim case; a scan that hits `limit` with more rows available is a
  second, previously-unsignaled kind of truncation that this also now
  flags (`truncated: true`, no `omitted_count` since the total isn't
  known). Name-fallback answers (`matched_by: alt_name`/`fuzzy`, #373)
  never carry a cursor — that pool is small and paginating it honestly
  isn't worth the complexity. `geocode`, `within_distance`, `water_near`,
  `changes_in_area` and other list tools are out of scope for this change;
  the new `placeroot.cursor` module is a standalone, reusable helper so
  they can adopt it later.
- Published input schemas now carry a JSON Schema `enum` plus a stated
  effective default for parameters whose valid values used to live only
  in prose: `mode` (every tool that takes one — `route`, `isochrone`,
  `from_to`, `travel_time_matrix`, `optimize_route`, `ground_location`,
  `places_along_route`, `neighborhood_verdict`, `preferences`), `prefer`
  (`route`, `from_to`), `op` (`geometry_op`), and `operating_status`
  (`find_places`). Runtime validation is unchanged — these stay plain
  strings, not `Literal`s, so an unsupported value still reaches the tool
  and returns its own structured self-correcting error (e.g.
  `unsupported_mode` with `supported: [...]`) instead of being rejected
  by schema validation before the call runs (roadmap docs/ROADMAP.md
  §4.3). Redundant value-listing prose was trimmed from several
  docstrings now that the enum carries it, keeping the net schema-token
  cost roughly flat.

- `find_places` gains `detail: "ids" | "compact" | "full"` and multi-
  category search (docs/ROADMAP.md §4.5). `detail="ids"` returns just
  `{id, distance_m}` — deliberately coordinate-free, for chaining an id
  into a batch lookup; `detail="compact"` returns `{id, name, category,
  lat, lon, distance_m, trust}`, where `trust` is a tier string
  (`"strong"|"ok"|"weak"|"unknown"`) derived from the exact same
  confidence/operating-status signals as `trust_note`'s prose (new
  `honesty.trust_tier`, alongside a payload-level `trust_legend` line so
  the tiers are self-explaining). compact keeps `lat`/`lon` so a composed
  `find_places` → `render_map` call still has coordinates to plot under
  the new default (#386); `detail="full"` is today's row, unchanged.
  Projection happens before the token budget is applied, so a smaller
  tier fits more rows per answer — the point of the feature.
  `categories: [...]` (up to 5 slugs, mutually exclusive with `category`)
  runs a checklist of several categories in one scan instead of one call
  per category, matched with the same substring/prefix semantics
  `category` already has; `group_by_category: bool` buckets the merged
  answer into `{category: [rows...]}`, up to `limit` rows per category
  from that same scan (a `row_number() OVER (PARTITION BY category ...)`
  window, not a second query per category). `detail` is presentation
  only — it is not part of a cursor's query identity, so a cursor issued
  under one detail tier continues correctly under another; `categories`
  (sorted for canonicalization) IS part of a cursor's identity, same as
  `category`. Grouped answers (`group_by_category=true`) never carry a
  cursor — each category is already `limit`-bounded from one scan; page a
  single one further with `category=<slug>` instead.
- `find_places` gains `within: {minutes, mode?, of?}` (docs/ROADMAP.md
  §4.2/§6E) — reachability-filtered search against the real street graph,
  not a radius guess: keeps only results inside the street-graph area
  reachable in `minutes` by `mode` from `of` (default: the search center;
  required — a `LocationRef` — in `division_id`/`area` mode, since there's
  no single center point there). Filtering happens in SQL
  (`ST_Contains` against the isochrone polygon, replacing the radius
  circle entirely — `radius_m` is ignored when `within` is set) so it
  composes unchanged with every existing filter, `detail` projection,
  cursor pagination, and `group_by_category`. A cold street graph returns
  `{"error": "needs_confirm", ...}`; retry with the new `confirm: bool`
  once the user agrees to wait (reuses the same gate `route`/
  `suggest_areas` already use). `of` given as a GERS id or name adds a
  compact `resolved` echo; the answer also carries a short honesty note
  naming the filter. `within`'s resolved `{minutes, mode, of lat/lon}` is
  part of a cursor's query identity, so a replayed cursor recomputes the
  same shed rather than trusting a stale one. Per-row travel minutes and
  an "excluded by radius but not by the shed" count were investigated and
  skipped for this PR (see the PR body) rather than shipped half-honest.

### Changed
- docs/ROADMAP.md's next-tier "Elicitation + sampling adoption" and "MCP
  roots as geofence" bullets corrected (#411): spec rev 2026-07-28 replaced
  server-initiated elicitation with Multi Round-Trip Requests (MRTR) and
  deprecated sampling and roots (SEP-2577; independently confirmed for
  roots while building #409). Investigated wiring MRTR into the
  `needs_confirm` cold-graph gates against the pinned SDK's
  `Resolve`/`Elicit` support (mcp 2.0.0) and stopped short of implementing:
  a `Resolve`-annotated parameter is dropped from the tool's published
  `inputSchema` and is always resolver-filled, so a capability-less client
  gets a hard `MCPError` instead of today's `needs_confirm` envelope —
  incompatible with keeping `confirm` client-suppliable for clients that
  don't support elicitation. No behavior change; docs-only.
- **Breaking: per-row batch errors now use the standard `{"error", "detail"}`
  envelope (docs/ROADMAP.md §4, next tier).** `geocode_batch`'s no-match
  rows changed from `{"query", "error": "no match"}` to `{"query", "error":
  "not_found", "detail": "no match for '...'"}`; `resolve_place_batch`'s
  no-match rows from `{"gers_id", "error": "not found"}` (note the space)
  to `{"gers_id", "error": "not_found", "detail": "no place matched GERS id
  '...'"}`; `reverse_geocode_batch`'s malformed-point rows from a bare
  prose string in `"error"` to `{"lat", "lon", "error": "bad_request",
  "detail": "<the old prose>"}`. An agent or downstream tool that
  string-matched the old bare `"error"` values (`"no match"`, `"not
  found"`, or the coordinate-range prose) must switch to checking `error
  == "not_found"` / `"bad_request"` and reading `detail` for the message.
  Row position/order and the outer `{"results": [...]}` shape are
  unchanged; only the value under each failed row's own `"error"` key
  moved to a machine-readable code plus a separate `"detail"` sentence,
  matching every single-call sibling's error shape.
- **Breaking: `find_places`' default answer shape changed.** Rows now
  default to `detail="compact"` instead of the full 13-field row (id,
  name, category, basic_category, operating_status, confidence, brand,
  has_website, has_phone, lat, lon, distance_m, trust_note) — an agent
  that read fields other than `id`/`name`/`category`/`lat`/`lon`/
  `distance_m` (e.g. `confidence`/`operating_status`/`brand`, or
  `trust_note`'s prose) must now pass `detail="full"` explicitly.
  Compact keeps `lat`/`lon` (unlike `ids`) specifically so the advertised
  `find_places` → `render_map` composition (#386) still renders under the
  default tier — ROADMAP §5.3's own output schema lists `lat`/`lon` as
  required row fields. This directly targets the token-efficiency
  benchmark's worst number: the committed `nearest_coffee` scenario drops
  from 811 to 470 response tokens, and the generic "1km, no filter"
  scenario from 824 to 463 (see `docs/benchmarks.md` /
  `docs/benchmarks-vs.md`, regenerated with this change). `find_near`
  passes `detail="full"` internally to keep its own existing projection
  working unchanged — it does not gain a `detail` param of its own in
  this PR (noted as follow-up scope). `find_places`' schema surface grew
  by 816 tokens (1692 → 2508) to publish `detail`/`categories`/
  `group_by_category`; the whole-install schema total moves from ~25,097
  to ~25,913 — a one-time cost that a single subsequent call already
  earns back given the per-call savings above.
- **Behavior change:** `from_to` and `travel_time_matrix` now consult the
  stored `mode` preference when `mode` is omitted, the same as `route`,
  `isochrone`, `optimize_route`, `ground_location`, and
  `places_along_route` already did. Previously both hard-defaulted to
  `"walk"` regardless of any stored preference, because their `mode`
  parameter defaulted to the literal string `"walk"` rather than `None`
  — `preferences.resolve_mode()` only consults the stored value when the
  caller's argument is `None`. An explicit `mode` argument still always
  wins.

### Fixed
- `from_to`'s published schema (the `FromToArguments` monkeypatch) used to
  silently drop `include_path`, `include_elevation`, and `prefer` even
  though the function has always accepted them — an agent reading the
  schema had no way to discover those three capabilities. All three are
  now published (docs/ROADMAP.md §4.3).
- `isochrone`'s concave boundary trace no longer collapses on sparse or
  evenly spaced street graphs (#389). A grid cell finer than the graph's
  own node spacing dropped every reached node into a cell of its own, and
  isolated cells share no sides — so the "longest boundary loop" was one
  arbitrary cell's square: a plausible-looking, tiny polygon reported for
  a large reach. `concave_boundary` now measures how much of the occupied
  set sits in one connected component and coarsens its cells until they
  join up, falling back to the convex hull if they never do. On the
  committed benchmark fixture the same 15-minute walk goes from a 0.02 km²
  sliver to a 2.9 km² walkshed; on a 300 m lattice, from a single 60 m box
  to the full 2.4 km span.
- Benchmarks re-captured accordingly: the published `isochrone_15min`
  answer size rises from 205 to 540 tokens, because the old number
  measured the degenerate polygon rather than a real one. `docs/benchmarks-vs.md`
  now says so beside the row instead of quietly booking the difference as
  efficiency.

## [0.9.9] — 2026-08-23

### Fixed
- Packaging: the published 0.9.8 wheel (42.7 MB) and sdist (87 MB) carried
  the *previous* Overture release's bundled artifacts alongside the pinned
  one — the pin bump added `2026-08-19.0`'s three artifact sets without
  removing `2026-07-22.0`'s, doubling every install for no benefit.
  Pruned: 0.9.9 installs at roughly half the size, with identical
  behavior (`bundled_artifact_release()` only ever reads the newest
  release present in all three sets).
- The same duplication pushed `site/placeroot.mcpb` to 42.8 MiB, over
  Cloudflare Pages' 25 MiB per-file limit, so every site deploy failed
  from the pin bump until it was pruned — the one-click Desktop Extension
  download and the site's version copy were stuck on the previous release.
  `docs/PIN.md` now requires deleting the superseded sets in the bump PR,
  and `tests/test_mcpb_bundle.py::test_site_bundle_fits_the_pages_file_limit`
  fails at merge time rather than after a tag.
- `Prepare Release` re-syncs `npm/README.md`: the version bump rewrites
  README's version-pinned links, which left the generated npm copy stale
  and failed the workflow's own verify step.
- `score_locality`'s requirement matching requires lexical-band confidence,
  so `search_categories`' embeddings tail (#356) — which matches almost
  any text at a deliberately low confidence — can no longer put a graded
  score behind a phrase the taxonomy never really matched. Unmatched text
  stays `measurable: false`, as #349 intends.

## [0.9.8] — 2026-08-22

### Added
- `suggest_areas`: inverse area search (#305 — #348/#349/#350). "Where
  within 25 min by bike of my office has parks and groceries?" — each
  anchor's isochrone shed, intersected when there are several (an area
  reachable from only one anchor is excluded), partitioned into candidate
  localities (`divisions_in_polygon`) and scored against free-text amenity
  requirements: exact/hierarchy category matching (never substring),
  0-1 scores with one-line reasons, and subjective asks ("quiet", "safe")
  returned `measurable: false` instead of silently scored. Stable GERS ids
  in results chain into `admin_lookup`/`summarize_area`; the same
  confirm/`needs_confirm` gate as `route` protects cold graph builds.
- `changes_in_area`: what opened and closed here between two Overture
  releases (#309 — #375/#376/#377). Live release enumeration, a GERS
  id-diff core (appeared / disappeared / changed places), and a ranked
  compact digest with honest framing that a disappearance may be
  delisting or dataset cleanup, not a closure.
- `verify_claims` + the `verify_listing_claims` prompt (#316): grade a
  listing's spatial claims ("8 min to the metro", "shops on the doorstep")
  against real routing and places data — confirmed / stretched / false /
  unverifiable, claimed vs measured. Category targets match exact
  taxonomy (with descendants), never substring — "park" does not confirm
  off a parking garage.
- `meeting_point`: travel-time-fair meeting spot for 2-5 people, each with
  their own mode — minimizes the worst-off participant's routed time,
  tie-broken by spread then total, returning ranked candidate venues with
  per-person times (#306).
- `elevation_at`: keyless ground elevation from Copernicus GLO-30 (~30 m),
  read straight from public cloud COGs — null with a note where there is
  no coverage (#358).
- `ground_location`: single-hop point grounding — where (reverse geocode),
  surroundings (500 m area summary), reach (isochrone stats), and the
  nearest named places, each section degrading independently to a note
  (#362).
- `geometry_op`: the offline geometry toolkit as one compact tool —
  buffer, centroid, area/length, point-in-polygon and friends over
  caller-supplied geometry, antimeridian- and pole-safe (#361).
- `render_map` explains, not just plots: point `class` + `legend` give
  pins contrasting colors and an on-page legend box (#367); shape
  `role: "shed"/"outline"`, `label`, and `callout` style and annotate
  polygons (#368); and composed tools now emit that vocabulary
  themselves — `compare_areas` (verdict mode) and `meeting_point` attach
  a `map` payload mirroring `render_map`'s own arguments, so
  `render_map(**result["map"])` renders the tool's argument in one call
  (#369).
- `compare_areas` priorities: weight the comparison by stated criteria
  ("parks matter double") and get an honest verdict — exact/hierarchy
  category counts, never substring, and a null verdict when the data is
  too degraded to score (#304).
- `search_categories` understands phrase intents ("fix my cracked phone
  screen" -> `mobile_phone_repair`): a curated synonym lexicon plus a
  token-coverage lexical fallback, every row carrying `confidence` (#355).
- Typo-tolerant and alt-spelling POI name search: fuzzy matching
  (Jaro-Winkler-gated) and locale alternate names extend `find_places` /
  `geocode` name lookups without leaking fuzzy rows into plain geocode
  results (#373).
- Quarterly listings-health check: a scheduled workflow probing the MCP
  registry entry (and that its version is not behind the repo), the
  directory listings, and that `uvx placeroot` still installs — filing or
  refreshing an issue on drift, closing it when clean (#254).
- `route`/`from_to`: comfort-aware routing (#313). `include_elevation`
  attaches a compact climb profile (`total_climb_m`, `total_descent_m`,
  `max_grade_pct`, a downsampled `samples` array) sampled from the
  Copernicus GLO-30 reader (#358), bounded to at most 40 lookups per route
  and fitted to the token budget; missing DEM coverage is reported as an
  honest note, never a fake 0.0. `prefer="flat"` (walk/cycle only)
  reweights the search to trade distance for climb, using per-node
  elevations fetched for the extracted subgraph (bounded to 400 lookups);
  it falls back to plain-distance routing with a note when elevation data
  isn't reachable. Documented explicitly: `prefer="flat"` minimizes grade
  only — it is not a step-free, stroller-, or wheelchair-accessible mode,
  since Overture's transportation schema carries no step-count or
  kerb-ramp attributes to support that claim honestly.
- `search_categories` embeddings tail extension (#356): a bundled, static
  hashed n-gram embedding artifact (~300 KiB, no model/network/API key)
  extends the phrase-intent fallback from #355 to queries that share no
  word with any slug, path segment, or synonym at all ("somewhere calm to
  read for an hour" -> `library`). Blended in only at a confidence below
  every lexical band, so it fills slots the lexical tiers left empty and
  never outranks a lexical hit — measured on a 28-query held-out corpus:
  10/10 lexicon-covered control queries unchanged, long-tail hit rate
  8/18 -> 11/18, total 18/28 -> 21/28. Falls back cleanly to lexical-only
  if the artifact is missing.
- `travel_time_matrix`: routed travel time and distance between every
  origin and every destination (up to 5x5) in one call, by mode — a shared
  cached street graph with one Dijkstra per origin when the points fit one
  extraction circle, a per-pair `route()` fallback when they don't.
  Unroutable pairs come back as null cells with a note instead of failing
  the matrix (#360).

### Fixed
- `resolve_place` city-hint resolution prefers a locality/localadmin over a
  same-named region (so "Times Square New York" is not anchored on New York
  *state*), and division candidates are graded against the better of the full
  query and the city-stripped query. Famous landmarks with no divisions-theme
  row keep using the bundled alias table (`Heathrow Airport` added).
  `geocode()` general ranking is unchanged (placeroot #344, #345 / #346).
- Routing to a POI centroid inside a large campus (an airport terminal, a
  park) no longer fails with `no_graph_nearby` when the real road network
  sits just past the snap radius: `snap_to_graph` makes one widened pass
  (4x the radius) for a usable-component node when everything closer is a
  tiny disconnected fragment. Measured at Austin–Bergstrom: nearest
  main-network node 455m out, everything within 300m a 1–4 node
  service-road sliver. Genuinely off-network points still return the error.
- `from_to` resolves its two names in parallel for real: `_resolve_pair`'s
  workers used to serialize on the shared DuckDB connection lock, so the
  cold name-pair peek cost two full resolves back to back (measured 17.3s
  on the c15 corpus walk, ~9s after). Each worker now runs on its own
  cursor of the shared instance via `db.isolated_reads()` (#328).
- The query-corpus worker imports the server before starting a leg's
  clock: an MCP server pays module import once at startup, not per
  question, and route peeks were reporting ~1s of interpreter import as
  question latency.
- Confirm-path review (#338): a disk graph peek now parks the loaded graph
  in the in-memory LRU (no second unpickle on the follow-up `route()`);
  `from_to` parallel resolves copy the request progress context into each
  worker; a `confirm=true` cold graph build that exceeds 2x the advertised
  ETA (`GRAPH_BUILD_S`) returns `eta_exceeded` instead of hanging. Warm
  cache hits stay uncapped.

### Changed
- Pinned Overture release bumped to `2026-08-19.0`; all three bundled
  artifact sets (file manifests, geocode index, land-cover grid)
  regenerated against it — no schema drift (#372).
- The tool surface grew 34 -> 42 across this release's features; every
  count and schema-token figure in `docs/REFERENCE.md`, the READMEs, and
  the site was remeasured from the live registry (42 tools, ~24.4k
  schema-token full surface — subset profiles and `progressive` keep the
  same savings story).
- `server.json`'s registry description is count-free ("Grounds AI agents
  in Overture Maps open data — compact answers, no API key.") and a guard
  test keeps any tool count from rotting in it again (#366).
- Profile token table in `docs/REFERENCE.md` remeasured after #336 confirm fields: all 34 tools stay ~15,711; subset rows follow the same ruler (search ~7,113, core ~7,569). Catalog rows for `route`, `from_to`, and `warmup_city` now name `confirm` / `needs_confirm`, plus `status`/`progress` on long answers.
- Question-gate route ids go through `server.route` / `from_to` so a
  valid `needs_confirm` is outcome `ask`, not a 15s fail and not a
  wrong place. Cold t01/c15 without confirm is the peek; `--warm` is
  `confirm=true`. Same `--fail-on both` commands (#336).
- Profile token table in `docs/REFERENCE.md` remeasured from `list_tools()` after #335/#334: all 34 tools are ~15,458 (was ~15,434); subset rows and the 96% savings claim follow the same ruler.
- Name path (#329): parse a trailing city (or reuse the last good one) as
  `resolve_place(city=)`, cache last resolve in-process keyed by
  (normalized query, effective city/coords including the implicit last
  city), and run `geocode_batch` against one
  shared divisions name table plus a tiny alias list on the bundled
  stage-0 index. Famous one-word POIs no longer lose to a random exact
  division (Colosseum → Queensland, Ebisu → Shikoku). Wrong place is
  still a fail even at 200ms.

### Added
- `route`, `from_to`, and a first `warmup_city` ask before a hop we already know
  will take 15+ seconds: a cold street-graph build returns
  `needs_confirm` in well under 500ms with an honest 5–25s ETA unless
  the caller passes `confirm=true` after the user agreed to wait. A
  warm or cached graph never asks. Long answers also carry `status`
  and a short `progress` list so a host without a progressToken can
  still show what is happening (#336).
- `from_to` and `find_near`: named-place compose so a walk or an
  "X near Y" is one tool hop. Resolves names inside the server
  (A and B in parallel), reuses `route` / `find_places`, and refuses
  a city-apart pair before any continent graph is built (#328).
- Auto-warm on first city-scale resolve: a successful `geocode` /
  `resolve_place` / `resolve_area` of a locality (not a POI, street, or
  address) starts background tile prewarm through the existing
  `warmup_city` path. Walk street graphs persist next to tiles — not
  inside the tile LRU — and survive process restart. Tiles are not a
  built graph; the first walk still builds or loads one (#330).
- Question-level 15s ship gate: 20 corpus ids, cold then warm, clock on
  the whole user question (all hops). Fails on a wrong place and on
  timeout; 10s is a stretch column, not a fail. Weekdays 15:00 UTC
  (08:00 PDT / 07:00 PST);
  PR authors prove a change with `--smoke --warm --budget-s 15
  --fail-on both` (#331).
- `route` and `optimize_route` now return an `export` object: Google/Apple
  Maps directions links (URL schemes only — no API, no keys, no extra
  network), a GPX 1.1 document, and a printable stop list, so a Saturday
  plan leaves the chat as something navigable (#312).
- `render_map` writes a shareable one-pager: interactive map, composed
  verdict, per-stop details, and the required Overture/OSM attribution —
  one local HTML file, no hosting, no account, no network (#310). Pass
  `summary` for the verdict you want on the page; a short fallback is
  composed from the payload when it's omitted. The three workflow
  prompts now finish by calling `render_map()` so the user leaves with
  a file they can send.
- `neighborhood_verdict`: a composed life-decision report — location plus
  free-form household/mobility context in, a ranked verdict (strengths,
  weak points, one thing to verify in person) out. One pass over
  `summarize_area`, a multi-slug places scan, and `isochrone`; no new
  remote APIs. Also a thin `should_i_live_here` prompt that tells the
  agent to call it (#303).
- Persistent local preferences: state "I bike everywhere, I have a dog"
  once. Exposed as `placeroot://preferences` and a small `preferences`
  tool. Routing tools use the stored mode when you omit theirs; an
  explicit argument always wins. Nothing leaves the machine (#315).
- Optional `warmup_city` — a "get to know my city" first-session step that
  pre-caches a metro through the existing tile cache so the first real
  question is fast. Cold scans now carry an honest ETA on MCP progress
  notifications. A `get_to_know_my_city` prompt walks the same path
  (#314).
- Actionable place rows now include a `trust_note`: a short before-you-go
  line from existing confidence and operating status. Composed itineraries
  add `verify_before_going` naming the 1–2 weakest-confidence stops
  (#308, #323).

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

### Changed
- Docs caught up with what shipped (#287).

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
