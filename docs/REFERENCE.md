# PlaceRoot reference

The full tool catalog, workflow prompts, resources, and the `PLACEROOT_TOOLS`
selection mechanism. For a quick overview, start with the [README](../README.md).

## All 35 tools

Every tool returns a compact, budgeted answer. Several single-item tools have a
`*_batch` sibling that collapses many calls into one round-trip.

| Tool | Answers |
|---|---|
| `find_places` | Named places near a point **or inside a named area / division polygon**, nearest first — filter by category, brand, confidence, operating status, or has-website / has-phone; each result carries a compact trust note |
| `summarize_area` | What's in an area: total places and top categories |
| `compare_areas` | 2–5 areas side by side: category mix, density, and what differs most — optional weighted `priorities` add a scored verdict with reasons |
| `within_distance` | Is the nearest matching place within N meters of a point? |
| `distance_matrix` | Straight-line distances between many origins and destinations at once |
| `place_details` | One place in full: addresses, contacts, brand, sources, confidence, and a compact before-you-go trust note |
| `admin_lookup` | The admin hierarchy containing a point: neighborhood up to country |
| `changes_in_area` | What's opened or closed around here since a past Overture release: headline counts, top names by category, and an honest note that a disappearance may be delisting/cleanup rather than a closure |
| `summarize_buildings` | Building stock in an area: count, footprint area, height and use mix |
| `buildings_at` | Nearest building footprints to a point |
| `land_use_at` | What kind of land is this: land use and land cover classification at a point |
| `infrastructure_at` | Infrastructure near a point, nearest first — filter by `subtype`/`infra_class` (e.g. `bridge`, `tower`) to see past the street furniture |
| `water_near` | Water near a point, nearest first — is this waterfront, how far to the nearest river/canal/lake; filter by `subtype`/`water_class` |
| `geocode` | Free-text place name → ranked candidates with coordinates and admin context (`geocode_batch` for many at once) |
| `geocode_batch` | Many free-text place names → one best match each, one round-trip |
| `resolve_place` | Free-text place reference → stable ids an agent can hold onto across turns |
| `resolve_place_batch` | Many GERS ids → compact place rows (batched `place_details`), one round-trip |
| `reverse_geocode` | Point → nearest address plus its containing admin areas (`reverse_geocode_batch`) |
| `reverse_geocode_batch` | Many points → nearest address plus containing admin areas, one round-trip |
| `address_at` | Point → the nearest street addresses (number, street, unit, postcode), with an explicit note when the country is outside Overture's 39-country address coverage |
| `geocode_address` | Street address → coordinates: "1600 Amphitheatre Parkway, Mountain View" — city-bounded, deduplicated to distinct number+street, nearest first |
| `gers_lookup` | Any GERS id → the entity it names (place, division, or building), what it's inside, and the building at its point |
| `search_categories` | Free text (including phrase intents like "fix my cracked phone screen") → ranked Overture category slugs with confidence, to filter `find_places` by |
| `isochrone` | The area reachable within N minutes on foot, bike, or car |
| `route` | Shortest-path distance and duration between two points, on foot, bike, or car; `include_path=true` adds the simplified route polyline; also returns an `export` object (Google/Apple Maps links, GPX, printable stop list). Optional `confirm` (bool, default false): a cold street-graph build returns `needs_confirm` with an ETA instead of blocking |
| `from_to` | Named-place walk/cycle/drive: resolve A and B in parallel, one graph, same shape as `route` including `export` maps/gpx/text. Fails with `too_far` if the ends are a city apart. Same optional `confirm` / `needs_confirm` gate as `route` |
| `find_near` | Category near a named place or city — one hop for "coffee shops near the Eiffel Tower"; compact rows with `trust_note` |
| `places_along_route` | Places on the way from A to B: corridor search along the route, with each result's detour and how far along it sits, compact trust notes on results, and a verify-before-going line for the weakest stops |
| `neighborhood_verdict` | Should I live here? A ranked verdict from life context (household, mobility, priorities) — strengths, weak points, one thing to verify in person |
| `optimize_route` | Best order to visit 2–10 stops, solved exactly over the street graph — order, per-leg distance/duration, totals, an `export` object (Google/Apple Maps links, GPX, printable stop list), and `verify_before_going` when stops already carry confidence/operating_status |
| `render_map` | Any result → a shareable one-pager (interactive map, verdict, stop list, and Overture/OSM attribution); optional `summary` for the verdict, otherwise a short fallback is composed from the payload |
| `simplify_geometry` | Any geometry → simplified to fit a token budget |
| `warmup_city` | Pre-cache a city's places and transportation tiles so later place searches over it read locally — does not build the street graph or cache buildings. A first tile COPY without `confirm` returns `needs_confirm` |
| `data_version` | Which Overture release the answers are drawn from |
| `preferences` | Read or update local travel/household defaults (mode, pace, dog, …). Nothing leaves the machine |


## Confirming a slow hop

`route`, `from_to`, and a first `warmup_city` take optional `confirm` (bool, default false). A cold street-graph build or a first tile COPY returns immediately:

```json
{"error":"needs_confirm","eta":"about 5–25 seconds","eta_s":[5,25],"detail":"Ask the user if they want to wait, then call the same tool again with confirm=true."}
```

Ask the user, then call the same tool again with `confirm=true`. A warm or on-disk cached graph never asks. Long answers also carry `status` (string) and `progress` (a short list of phase strings) so a host without a progressToken can still show what is happening. Fast lookups stay clean.

## Tool annotations and listing cache hints

Every tool declares MCP annotations — closed-world, plus a human-readable
title, and `readOnlyHint: true` on the 33 that are pure lookups — so a client
can tell before it prompts you which calls touch nothing. Two tools write
locally: `render_map` writes an HTML file, and `preferences` writes a local
JSON document, so both declare `readOnlyHint: false`. Honest caveat: Claude Code does not gate its permission
prompts on `readOnlyHint` (it uses its own classifier), so the practical win is
with clients that do, such as Codex CLI and Copilot-class agents — plus spec
hygiene everywhere else.

PlaceRoot also speaks the MCP 2026-07-28 revision's listing cache hints:
`tools/list` (and the prompt and resource listings) come back with
`ttlMs: 86400000` and `cacheScope: "public"`. The listings are frozen at build
time — nothing at runtime can change them — so a client is free to reuse them
for a day instead of re-reading a ~17.7k-token schema surface every session.
Clients that speak an older revision are unaffected: those fields did not exist
before 2026-07-28, and the response they get is byte-identical to what it was.

## Workflow prompts

Five MCP **prompts** ship with the server: canned multi-tool workflows that
encode which tool to call first, what to do with its output, and what the
answer should look like. In Claude Code they appear as slash commands; Claude
Desktop and Cursor surface them in their own prompt pickers.

| Prompt | Arguments | Workflow |
|---|---|---|
| `/mcp__placeroot__site_selection` | `business_type`, `area` | `search_categories` → `geocode` → `summarize_area` → `compare_areas` → `find_places` + `within_distance` → one ranked recommendation → `render_map()` so the user leaves with a file |
| `/mcp__placeroot__compare_neighborhoods` | `area_a`, `area_b` | `geocode`/`resolve_place` + `admin_lookup` → `summarize_area` ×2 → `compare_areas` → `summarize_buildings` → a small difference table → `render_map()` so the user leaves with a file |
| `/mcp__placeroot__plan_errands` | `stops`, `start` (optional) | `geocode_batch` → `distance_matrix` → `route` per leg → optional `places_along_route` → an ordered run with per-leg distance and duration, plus a verify-before-going line for the weakest 1–2 stops → `render_map()` so the user leaves with a file |
| `/mcp__placeroot__should_i_live_here` | `location`, `context` (optional) | `geocode` (if needed) → `neighborhood_verdict` → a verdict, strengths, the weak point, and the one thing to verify in person |
| `/mcp__placeroot__get_to_know_my_city` | `city` (optional) | `warmup_city` → pre-cache the metro so the first real question is fast |

```
/mcp__placeroot__site_selection bike repair shop | Portland, Oregon
```

Prompts cost **zero tokens in `tools/list`** — a client fetches them only on
`prompts/list`, and only materializes one when you invoke it. A test asserts
`tools/list` is byte-identical with and without them registered, so adding a
workflow here can never grow the context every conversation pays for.

They are also registered under **every** `PLACEROOT_TOOLS` selection, including
subsets that drop tools a prompt names — a workflow is still worth reading when
one step is unavailable, and it costs a subset install nothing. When the active
selection excludes a referenced tool, the rendered prompt ends with a note
naming it and telling the agent to route around the gap rather than to attempt
a call that would fail.

## Resources

Three MCP **resources** expose the server's argument-free lookups as attachable
context, so you can pin them into a conversation without spending a tool call:

| Resource | Contents |
|---|---|
| `placeroot://data-version` | The resolved Overture release, its date, how it was resolved (discovery, env override, the pinned fallback, or held at the artifact release), its age, and whether the bundled acceleration applies to it. Same values the `data_version` tool returns — one shared code path, so they cannot drift. |
| `placeroot://preferences` | Local travel and household preferences (mode, pace, household). Same document the `preferences` tool reads and updates — one shared code path. Nothing in this file leaves the machine. |
| `placeroot://categories` | Summary of the place-category taxonomy: all 22 top-level categories with how many slugs sit under each, plus how to get an exact slug. ~530 tokens — a summary, not the 2,117-slug CSV, which stays behind `search_categories`. |

In Claude Code they auto-complete as @-mentions:

```
@placeroot:placeroot://data-version
@placeroot:placeroot://preferences
@placeroot:placeroot://categories
```

Claude Desktop and Cursor list them in their own attachment pickers.

Like prompts, resources are registered under **every** `PLACEROOT_TOOLS`
selection: they never appear in `tools/list`, so gating them would save a
subset install nothing. A test asserts `tools/list` is byte-identical with and
without them registered.

## Loading fewer tools (`PLACEROOT_TOOLS`)

All 35 tool schemas cost roughly **17.7k tokens** of every conversation's
context, paid before the agent asks anything — about the cost of 85 median
answers. Most installs use a slice of that surface, so `PLACEROOT_TOOLS`
selects which tools get registered. Unselected tools are never registered and
never appear in `tools/list`.

```json
{
  "mcpServers": {
    "placeroot": {
      "command": "uvx",
      "args": ["placeroot"],
      "env": { "PLACEROOT_TOOLS": "core" }
    }
  }
}
```

The value is a comma-separated list of profile names, tool names, or both — the
union of everything named:

| `PLACEROOT_TOOLS` | Tools | Schema tokens | Saved |
|---|---:|---:|---:|
| unset / `all` (default) | 35 | ~17,737 | — |
| `search` | 15 | ~7,173 | 60% |
| `core` | 15 | ~7,629 | 57% |
| `routing` | 8 | ~3,941 | 78% |
| `analysis` | 12 | ~5,899 | 67% |
| `geometry` | 4 | ~1,265 | 93% |
| `progressive` | 4 (all 35 reachable) | ~866 | 95% |

- **`core`** — `find_places`, `geocode`, `reverse_geocode`, `place_details`, `resolve_place`, `search_categories`, `summarize_area`, `route`, `from_to`, `find_near`, `places_along_route`, `neighborhood_verdict`, `warmup_city`. The single-purpose tools that answer most spatial questions; no batch siblings, no buildings/land-use, no rendering. `search_categories` is in for its own reason: `find_places`' `category` filter takes Overture taxonomy slugs, and a wrong slug comes back as zero results plus a note to look the slug up — a dead end without the lookup tool to call.
- **`search`** — the find/name/identify family: `find_places`, `find_near`, `place_details`, `geocode`, `resolve_place`, `reverse_geocode`, their `*_batch` siblings, `address_at`, `geocode_address`, `search_categories`, and `gers_lookup`.
- **`routing`** — `route`, `from_to`, `isochrone`, `distance_matrix`, `within_distance`, `optimize_route`.
- **`analysis`** — `summarize_area`, `summarize_buildings`, `compare_areas`, `buildings_at`, `land_use_at`, `infrastructure_at`, `water_near`, `admin_lookup`, `neighborhood_verdict`, `changes_in_area`.
- **`geometry`** — `simplify_geometry`, `render_map`.
- **`progressive`** — not a slice of the surface but a door to it: `placeroot_capabilities()` returns a ~1,000-token catalog of all 35 tools (name, one-liner, argument list), and `placeroot_call(tool, args)` runs any of them and returns the tool's own answer unchanged. For the install that wants everything available without paying 17.7k tokens for it in every conversation — profiles need you to know up front which tools you want; this doesn't. One extra round trip when the agent needs the catalog. It replaces the surface rather than adding to it, so it has to stand alone: `PLACEROOT_TOOLS=progressive,core` fails at startup rather than registering both.

`data_version` and `preferences` are registered under every profile.
`data_version` is ~230 tokens and the only way an agent can tell which
Overture release backs its answers. `preferences` is the local defaults
document; routing tools read its stored mode when theirs is omitted.

Profiles may overlap, and a list may mix them with bare tool names —
`PLACEROOT_TOOLS=routing,find_places` or
`PLACEROOT_TOOLS=find_places,geocode,route`. A name that is neither a profile
nor a tool **fails at startup** with the list of valid names, rather than
quietly falling back to loading everything. The server logs one line at
startup naming what it registered
(`registered 15 of 35 tools (PLACEROOT_TOOLS=core)`), so a selection that
didn't apply — an empty value, a variable that never reached the process — is
visible rather than silently the full 35.

## What it deliberately does not do

Overture's open data does not carry these, so PlaceRoot does not pretend to:

- **No live traffic.** Routing is free-flow; durations do not reflect current conditions.
- **No opening hours.** Places carry categories, brands and contacts, not schedules.
- **No ratings or photos.** There is no review corpus and no imagery behind these answers.

If a question needs one of those three, a commercial maps API is the right
tool. For where things are, what is around them, and what is reachable from
them, PlaceRoot answers without a key.

## Configuration

Nearly every setting is an environment variable; none is required. (The
transport itself is the exception — `--host`/`--port` are CLI flags, not
env vars; see below.) The ones an operator is most likely to reach for:

| Variable | Default | What it does |
|---|---|---|
| `PLACEROOT_TOOLS` | all 35 | Tool profile/subset — see the section above |
| `PLACEROOT_TOKEN_BUDGET` | `2000` | Soft per-response token budget (chars/4 heuristic); rows are dropped lowest-ranked first, then optional fields, until a response fits |
| `PLACEROOT_RECREATION_LAYER` | on | `0`/`false`/`no`/`off` disables the base-theme recreation layer ([docs/RECREATION.md](RECREATION.md)) |
| `PLACEROOT_CACHE` | on | `off` disables the local tile cache entirely |
| `PLACEROOT_CACHE_DIR` | `~/.cache/placeroot` | Where tiles, persisted walk graphs, the geocode name index, and support tables live |
| `PLACEROOT_ARTIFACT_DIR` | sibling of `PLACEROOT_CACHE_DIR` (`.../artifacts`) | Where `render_map` writes its self-contained HTML files |
| `PLACEROOT_CACHE_MAX_MB` | `500` | LRU size cap for the cache directory |
| `PLACEROOT_CACHE_SYNC` | off | Materialize missing tiles inline instead of in the background (tests, warm-starts) |
| `PLACEROOT_CACHE_FETCH_CONCURRENCY` | `2` | Concurrent background tile fetches — bounded so a cold query's own scan isn't starved by its cache warmers |
| `PLACEROOT_DUCKDB_THREADS` | `96` | DuckDB threads; deliberately above core count because cold parquet-footer reads are IO-bound |
| `PLACEROOT_WARM_REGION` | unset | `lat,lon,radius_m` to pre-warm at startup, on top of the automatic metadata pre-warm |
| `PLACEROOT_DATA_PATH` / `PLACEROOT_DATA_PATH_<THEME>` | unset | Pin a theme to a local dataset instead of live S3 |
| `PLACEROOT_UPSTREAM_BASE` | Overture's public bucket | Point every theme at a mirror in the standard release layout ([docs/MIRROR.md](MIRROR.md)) |
| `PLACEROOT_S3_REGION` / `PLACEROOT_S3_ENDPOINT` | `us-west-2` / AWS | Mirror plumbing: region, custom S3-compatible endpoint (credentials via `PLACEROOT_S3_ACCESS_KEY_ID`/`SECRET_ACCESS_KEY`) |
| `PLACEROOT_RELEASE_TTL_HOURS` / `PLACEROOT_STALE_RELEASE_DAYS` | `6` / `60` | Release re-discovery cadence and the staleness warning threshold |
| `PLACEROOT_TRACE` / `PLACEROOT_TRACE_SLOW_S` | off / `10` | `PLACEROOT_TRACE=1` logs each call's per-phase breakdown; any call slower than the threshold carries the same breakdown in its response as `timing`, so a slow answer explains itself (`0` disables the response field) |

Cold-query behavior worth knowing: the first query over a new area fetches
that area's tiles (narrated via MCP progress when the client sends a
`progressToken`); repeat queries answer from the local cache in
milliseconds. Resolving a city-scale place also starts background tile
warming for that metro — users never have to call `warmup_city`. Tiles
are not a built street graph: the first walk still builds or loads the
graph; later walks reuse the on-disk graph across process restarts.
Wheel-bundled per-release manifests keep even the first query's scan to
the few files its bounding box intersects.

**Which Overture release you get.** Three bundled artifact sets — the file
manifests, the stage-0 geocode index and the coarse land-cover grid — are
keyed by release, and they are what makes a cold query cost seconds instead
of tens of seconds. They apply to exactly one release and miss (harmlessly,
never wrongly) on any other. So when discovery finds a release newer than
the one this build ships artifacts for, PlaceRoot **reports it rather than
adopting it**, and keeps answering from the release it can answer fast on.
Once that release passes `PLACEROOT_STALE_RELEASE_DAYS` (default 60, two
missed Overture releases) freshness wins instead: the newer release is
adopted, the logs say why, and cold queries are slower until the package is
upgraded to a build whose artifacts match.

`data_version` reports this as `artifacts: matched | unmatched`, alongside
`newer_release` when one is being deliberately passed over. To take the
newest data immediately and give up the bundled acceleration, set
`PLACEROOT_OVERTURE_RELEASE` to that release — an explicit override always
wins.
