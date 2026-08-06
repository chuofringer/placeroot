# Site selection: where should I open a coffee shop in Austin?

A worked example of PlaceRoot's flagship use case — a real site-selection
question, answered entirely through PlaceRoot's MCP tools, no vendor API, no
ETL, no spreadsheet of scraped listings. Every number below came back from a
single MCP tool call.

**Status note:** the pipeline runs against live Overture S3 by default
(verified end-to-end — see #17); `--offline` is the CI-safe fallback. The
transcript quoted below is from an `--offline` run against the committed
fixture so it's reproducible. The tool sequence, token math, and
recommendation logic are the same in both modes — only the specific numbers
(and the fact that "Austin" becomes a synthetic test fixture) differ.

## The question

Three commercial districts in Austin are common answers to "where's a good
spot for a coffee shop": **South Congress** (walkable retail strip), **East
Austin** near E 6th / Cesar Chavez (dining and nightlife corridor), and
**The Domain** (suburban office-park retail hub). Which one actually has the
best mix of low existing competition and reachable daily-need foot traffic?

## Running it

```bash
uv sync
uv run python examples/site_selection/run_demo.py            # live Overture S3
uv run python examples/site_selection/run_demo.py --offline  # committed fixtures, no network
```

The script (`run_demo.py`) is a real MCP client — it launches `uv run
placeroot` as a subprocess over stdio, using the `mcp` SDK's
`stdio_client` + `ClientSession`, the same transport a real agent would use.
`--offline` points the query layer at the committed test fixtures
(`tests/fixtures/places.parquet`, `tests/fixtures/divisions.parquet`) via the
same env vars `tests/conftest.py` uses, so `tests/test_demo.py` can smoke-test
the whole pipeline in CI with no network.

## The tool sequence

1. **`summarize_area`** on each of the 3 candidate centers — category mix and total place count.
2. **`compare_areas`** across all 3 at once — aligned category counts, density, and differentiators.
3. **`find_places`** (`category=coffee_shop`) in each — existing competition, nearest-first.
4. **`within_distance`** (`category=grocery_store`) per candidate — is a grocery anchor within 600m? (Overture's places theme doesn't carry transit stops, so this and the next check stand in as "does this spot already have daily-need foot traffic" signals, not a real transit-access measure.)
5. **`within_distance`** (`category=gym`) per candidate — second daily-need anchor.
6. **`place_details`** on the closest coffee-shop competitor found in step 3, resolved by its GERS `id`.
7. **`admin_lookup`** on each candidate center — names the containing neighborhood/locality.

17 tool calls total, none of them returning more than a couple hundred tokens.

## Sample run (`--offline`, committed fixture)

Run on `2026-08-05` against `tests/fixtures/places.parquet` /
`tests/fixtures/divisions.parquet` (the same fixtures `tests/test_demo.py`
uses). The three "candidate centers" are just three nearby points inside the
fixture's single synthetic data cluster — this transcript demonstrates the
tool pipeline and token math, not a real Austin verdict.

```
# Connected to MCP server: placeroot

## Tool calls

1. summarize_area — summarize_area(Fixture Area A (southwest))
   args: {"lat": 40.698, "lon": -73.902, "radius_m": 800}
   response (~183 tokens): {"center": {"lat": 40.698, "lon": -73.902}, "radius_m": 800.0,
   "total_places": 201, "top_categories": [{"category": "bank", "count": 23},
   {"category": "grocery_store", "count": 23}, {"category": "coffee_shop", "count": 23},
   {"category": "restaurant", "count": 23}, ...]}

2. summarize_area — summarize_area(Fixture Area B (northeast))
   response (~183 tokens): total_places: 202, same top categories, same shape.

3. summarize_area — summarize_area(Fixture Area C (center))
   response (~182 tokens): total_places: 202.

4. compare_areas — compare_areas(all 3)
   args: {"areas": [{"lat": 40.698, "lon": -73.902}, {"lat": 40.702, "lon": -73.898},
   {"lat": 40.7, "lon": -73.9}], "radius_m": 800}
   response (~619 tokens): per-area total_places/density_per_km2/category_counts aligned
   across all 3, plus a "differentiators" list ranked by relative difference.

5-7. find_places — find_places(coffee_shop near <each area>)
   args: {"lat": ..., "lon": ..., "radius_m": 800, "category": "coffee_shop", "limit": 10}
   response (~764-768 tokens each): 10 nearest coffee_shop rows, e.g.
   {"id": "6521ea65d7c078046526c047fc647384", "name": "Cluster Place 016",
   "category": "coffee_shop", "operating_status": "open", "confidence": 0.68,
   "distance_m": 65.0}

8-13. within_distance — grocery_store and gym anchors per area
   e.g. within_distance(grocery near Fixture Area A): {"within": true,
   "nearest": {"id": "4341199e...", "name": "Cluster Place 018",
   "category": "grocery_store", "distance_m": 89.0}, "distance_m": 89.0}
   (~79-84 tokens each)

14. place_details — place_details(closest competitor: Cluster Place 160)
   args: {"id": "68cf6f90c6fddf43e71f6a409cca2b99"}
   response (~84 tokens): {"id": "68cf6f90...", "name": "Cluster Place 160",
   "category": "coffee_shop", "operating_status": "open", "confidence": 0.67,
   "brand": null, "addresses": [], "websites": [], "phones": [], "socials": [],
   "sources": [], "lat": 40.699798, "lon": -73.899751}

15-17. admin_lookup — admin_lookup(<each area>)
   response (~149 tokens each): {"chain": [{"name": "Downtown", "type": "neighborhood",
   "id": "2835be08..."}, {"name": "Metropolis", "type": "locality", ...},
   {"name": "Franklin County", "type": "county", ...}, {"name": "Empire State",
   "type": "region", ...}, {"name": "United Testland", "type": "country", ...}]}

## Token accounting

  summarize_area (x3)                                     ~183, ~183, ~182 tokens
  compare_areas(all 3)                                    ~619 tokens
  find_places(coffee_shop) (x3)                           ~764, ~765, ~768 tokens
  within_distance(grocery) (x3)                           ~84 tokens each
  within_distance(gym) (x3)                               ~79 tokens each
  place_details(closest competitor)                       ~84 tokens
  admin_lookup (x3)                                        ~149 tokens each

  TOTAL across 17 tool calls: ~4484 tokens

## Recommendation

- Downtown (Fixture Area A): 10 existing coffee_shop competitors within 800m,
  grocery anchor within 600m, 201 total places nearby.
- Downtown (Fixture Area B): 10 competitors, grocery anchor within 600m, 202 total places.
- Downtown (Fixture Area C): 10 competitors, grocery anchor within 600m, 202 total places.

Recommended: Downtown — fewest coffee_shop competitors (10) among candidates
with a grocery anchor in reach, and 202 total places nearby to draw walk-in
traffic from.
```

(The fixture's synthetic cluster deliberately repeats the same category mix
at every point — that's why the offline "recommendation" is a near-tie. The
live path against real Austin data would show real differentiation between
South Congress, East Austin, and The Domain; see the status note above.)

## Token math: answers, not data dumps

Every tool response above is a small, ranked JSON object — never a raw
GeoJSON feature collection. The script estimates each response's token cost
with the same heuristic PlaceRoot's own budget enforcement uses
(`len(json) / 4`, see `src/placeroot/budget.py`), so the numbers below are
directly comparable to the server's own 2,000-token-per-answer budget.

<!-- TOKEN_TABLE_PLACEHOLDER -->

For contrast: a single Overture GeoParquet row for one place carries ~15
columns including nested structs (`names`, `addresses`, `sources`,
`categories`, geometry) — raw, that's typically 800-1,500+ tokens serialized
as GeoJSON *per place*. `find_places` returning 10 ranked, deduplicated
results in ~750-800 tokens total is roughly what one or two raw GeoJSON
features would cost alone. Fetching "everything within 800m of 3 candidate
neighborhoods" as raw GeoJSON (hundreds of places per area, before any
filtering or ranking) would run tens of thousands of tokens — far past what
fits in a single tool response, and far past what an agent should have to
read to answer "which of these 3 areas has less coffee competition."

## Recommendation

<!-- RECOMMENDATION_PLACEHOLDER -->

The recommendation logic (see `_recommend()` in `run_demo.py`) is entirely
data-backed: it reads competitor counts from step 3, anchor reachability from
step 4, and total activity from step 1 — it does not fabricate a verdict, and
if any tool call fails or returns an `{"error": ...}`, the script stops
instead of guessing.

## Caveats

- `place_details(id=...)` (step 6) is a full-dataset scan when resolving by
  GERS id alone (documented in `overture.place_details`'s docstring) — fine
  against the small fixture or a warmed cache tile, materially slower cold
  against the full Overture release.
- The 3 candidate centers and the `grocery_store`/`gym` anchor categories are
  a reasonable first cut for this scenario, not a rigorous retail-siting
  model — real site selection would weigh rent, footfall counts, and
  demographics that aren't in Overture's places theme at all.
