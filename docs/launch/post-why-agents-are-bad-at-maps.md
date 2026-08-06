# Why agents are bad at maps

An agent asks a simple spatial question — "what's within 500 meters of this
point?" — and the answer comes back as a GeoJSON `FeatureCollection`. Every
feature carries a full geometry, a nested `sources` array with per-property
provenance and confidence, a `names` struct, a `categories` struct, contact
fields, and more. Multiply that by a few hundred features, which is a normal
count for a dense downtown block, and the response no longer fits in a
reasonable slice of a context window. The agent either truncates blindly,
burns most of its budget on one tool call, or the integration just doesn't
ship.

This isn't a hypothetical. It's now a named problem, in public, from a
company that ships production geospatial infrastructure: Mapbox's "GeoAI in
2026" post (December 2025) calls large geospatial payloads the biggest
challenge for MCP-based mapping integrations. A Geoawesome essay from June
2026 makes the shape of the problem concrete: a real-world GeoJSON payload of
roughly 45,000 tokens collapses to about 25 tokens once you return a
reference instead of the geometry itself. The gap between those two numbers
*is* the problem — and it's a gap most map data tooling was never designed to
close, because it was designed for GIS software and browsers, not for a
model with a token budget.

## Two structural camps

Look at what's available today for grounding an agent in map data, and it
splits into two camps, each solving a different half of the problem.

The first camp is vendor-keyed APIs. You get an API key, you get billing,
and in exchange you get curated, often well-ranked place data — sometimes
even AI-generated summaries. This solves the payload problem reasonably
well, because the vendor controls the response shape. What it doesn't solve
is the dependency: your agent's map grounding now lives behind a signup
flow, a metered price per call, and someone else's terms of service on
caching and training. (One current example: Google's Maps Grounding Lite is
priced at $14–25 per 1,000 prompts, with no-caching and no-training terms
attached.)

The second camp is open-data wrappers, most commonly built on OpenStreetMap
via Nominatim or Overpass. These are keyless, which solves the dependency
problem — but most of them are thin passthroughs: you get raw OSM tags or
raw GeoJSON back, at whatever size the underlying query produces. The payload
problem is unsolved, just moved from a paid API to a free one. You've traded
a metered key for a token budget that blows up on any moderately dense area.

Neither camp treats "fits in an agent's context window" as a first-class
design constraint. One buys their way around it with curation; the other
doesn't address it at all.

## What PlaceRoot does differently

PlaceRoot queries Overture Maps' public GeoParquet release directly with
DuckDB — no API key, no signup, no intermediate database, no ETL job. Row-
group pushdown on the bbox columns means a query only reads the tiles it
needs. That gets the *right* data quickly. It doesn't, by itself, make the
data small — a raw Overture Places record still serializes to several
hundred tokens per feature once you include the nested `sources`,
`categories`, `names`, and address structs. Making the payload problem
disappear is a separate design rule, applied to every tool: return an
answer, not a data dump. Every tool response is built to a token budget —
roughly 2,000 tokens per call — and when a result would exceed it, rows are
dropped nearest-first-preserved and optional fields stripped, with
`truncated: true` and an `omitted_count` reported explicitly rather than
silently cutting the response short.

The measured numbers back this up. `summarize_area`, run against a set of
1,944 places, returns a category-mix summary in about 320 tokens — three
orders of magnitude smaller than serializing those places as raw features
would cost. `find_places` returning ten ranked, deduplicated coffee shops
within 500 meters comes back at 501 tokens, against roughly 45,000 tokens
for the equivalent raw GeoJSON dump of every place in that radius (measured
at ~291 tokens per feature, times a normal all-category count for a dense
downtown block). The flagship worked example — a full site-selection
analysis across three candidate neighborhoods, chaining `summarize_area`,
`compare_areas`, `find_places`, `within_distance`, `place_details`, and
`admin_lookup` — runs a 17-call analysis end to end for roughly 5.9K tokens
total, live-verified against Overture. That's a complete multi-step spatial
analysis costing less than one raw GeoJSON dump of a single neighborhood.

Geocoding is built the same way, and on the same substrate: `geocode` and
`reverse_geocode` run entirely on Overture's `divisions` and `addresses`
themes, not on Nominatim or a paid geocoding API. Matching is deterministic
— exact name, then prefix, then substring, with population (or a documented
proxy when population is null) breaking ties within a match tier — so it's
transparent rather than a black-box ranking model. Measured against a live
Overture release across 113 real-world queries (cities, neighborhoods,
"City, ST" forms), it resolves hit@1 100% and hit@5 100% on that set, up from an
initial 35.4%/54.9% before two fixes: pushing match-tier and population
ordering into the SQL query itself (rather than after an overfetch limit —
122 divisions worldwide are literally named "Los Angeles," and a naive
overfetch can drop the well-known one before ranking ever sees it), and
adding "City, ST"/"City, Region" string parsing.

None of this requires an account. The first `geocode()` call in a process
materializes a local name table from Overture's division data once per
release (logged, ~20-30 seconds); every call after that, and every place
query touching a tile already seen, answers from a local cache instead of a
live S3 scan — measured around 21ms warm, versus a design target under 5
seconds cold. Turn caching off with `PLACEROOT_CACHE=off` if you want every
query to hit upstream directly.

Every place PlaceRoot returns also carries Overture's GERS id — a stable,
cross-release identifier for the real-world entity, not a row number. An
agent can hold onto one across turns, or persist it, and resolve the same
place later with `place_details(id=...)` instead of re-searching by name and
hoping the match lands the same way twice.

## What it doesn't do

Some honest limits, not smoothed over:

- **No hosted tier yet.** PlaceRoot runs as a local (or self-hosted)
  process; there's no managed, no-setup endpoint you point a client at
  today. `--http` mode exists and works, but it's your process, your TLS
  termination, your reverse proxy — nothing here is a managed service. A
  hosted tier is a tracked roadmap item, not shipped.
- **Routing is walking-first.** The routing stack builds its own graph from
  Overture's transportation theme rather than depending on a hosted routing
  service, but the mature path today is pedestrian routing and isochrones.
  Drive and cycle modes exist; walking is where it's been exercised most.
- **Benchmark caveats.** The 100% geocode hit rate is measured against 113
  queries on one live Overture release, not a standardized, versioned
  benchmark — a saturated task set means the next honest step is a harder
  set, and it is not a guaranteed number across every query shape or every
  future release. The
  45,000-token raw-GeoJSON comparison is a measured per-feature cost
  extrapolated to a typical dense-block feature count, not a fixed
  constant — actual raw payload size depends on how many places are nearby
  and how many categories you're not filtering out.

## Try it

```json
{
  "mcpServers": {
    "placeroot": {
      "command": "uvx",
      "args": ["placeroot"]
    }
  }
}
```

Or run it directly:

```bash
uv run placeroot
```

Source, tools, and the full design rules: https://github.com/chuofringer/placeroot
