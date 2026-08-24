# PlaceRoot agent-optimization roadmap

*Prepared 2026-08-24 against `main` (0.9.9, 42 tools, Overture `2026-08-19.0`).
Grounded in a full audit of `server.py`'s registered schemas, the published
benchmarks (`docs/benchmarks.md`, `docs/benchmarks-vs.md`), and an August 2026
survey of the competing geospatial MCP landscape.*

This is a product roadmap written from the consuming agent's chair, not the
operator's. The question throughout is: **for a given spatial question, how
many round-trips, how many tokens, and how many dead ends does an agent pay
today — and what single change removes the most of each?**

---

## 1. How an agent actually experiences PlaceRoot today

### What already works — and is genuinely rare

Credit first, because most of the "obvious" agent-optimization wishlist is
already shipped, and several items are unique in the market:

- **Batching** — `geocode_batch`, `reverse_geocode_batch`, `resolve_place_batch`,
  `distance_matrix`, `travel_time_matrix`. Batch operations are *essentially
  absent* from every other geospatial MCP surface (Mapbox's matrix tools are
  the closest).
- **Isochrones** — keyless, global, real street graph. Only Mapbox, Stadia,
  TomTom and the self-hosted Valhalla/ORS stack serve isochrones at all, and
  every one of them requires a key.
- **Stable IDs** — GERS ids in results, `gers_lookup`, ids that chain across
  tools and across turns.
- **Compound tools** — `find_near`, `ground_location`, `suggest_areas`,
  `meeting_point`, `places_along_route`, `optimize_route` each collapse what
  is a 3–6 call chain on competitor servers into one call.
- **Self-correcting errors** — `unsupported_mode` ships `supported: [...]`;
  `ambiguous_area` ships pickable `candidates`; `resolve_place` misses ship a
  machine-actionable `retry_with` sketch; category misses point at
  `search_categories`; `needs_confirm` names the exact retry. The market's
  norm is a bare `fetchWithTimeout` exception
  ([cyanheads/openstreetmap-mcp-server#32](https://github.com/cyanheads/openstreetmap-mcp-server/issues/32)).
- **Capabilities discovery** — the `progressive` profile (~866 tokens for the
  whole 42-tool surface) is the strongest answer in the field to the
  tool-schema bloat problem every agent developer complains about.
- **Honesty machinery** — `trust_note`, `truncated`/`omitted_count`,
  `degraded_fields`, `data_version`. Only Placematic even *markets*
  provenance; nobody else ships it.

So the roadmap below is not "add the basics." It is: fix the five places
where the current surface still taxes agents, and weaponize the honesty
machinery into a differentiator no keyed competitor can copy.

### Where the tax is paid

**Token cost per conversation.** The schema surface is 24,410 tokens for the
default install — larger than Mapbox's 15,190 on comparable fields, and the
repo's own benchmarks say so. The mitigations (`PLACEROOT_TOOLS`, listing
cache hints, `progressive`) are good, but the default an agent meets in the
wild is still the full 24.4k. Median answer is a healthy 208 tokens, but the
flagship `find_places` answers at 802–811 tokens (12 fields × up to 25 rows)
— 7× Mapbox's equivalent, and the single number quoted against us in
head-to-heads.

**Round-trips.** One category of hop dominates every workflow: **resolving a
name to coordinates before the tool that actually answers the question.**
The point-taking tools (`compare_areas`, `optimize_route`, `isochrone`,
`summarize_area`, `distance_matrix`, `meeting_point`, …) all require
`{lat,lon}`, so nearly every real conversation opens with a `geocode` /
`geocode_batch` call whose output the agent immediately re-types into the
next call — a full round-trip plus the token cost of echoing coordinates
through the context. `find_near` and `from_to` prove the fix works; it just
isn't uniform.

**Parameter ambiguity.** Five point-encoding conventions coexist (`lat`/`lon`
scalars; `from_lat`/`from_lon`; lists of `{lat,lon}` under six different key
names — `origins`, `destinations`, `areas`, `anchors`, `stops`, `points`;
`near_lat`/`near_lon` hints; `geometry_op`'s point objects). Enum-valued
parameters (`mode`, `op`, `prefer`, `operating_status`, `claims[].kind`) are
typed as free strings — the valid values live only in prose, so the first
wrong guess costs a round-trip to learn them from the error. And `mode`'s
default is a per-tool lottery, invisible in the schema: `route` defaults to
drive, `isochrone` to walk, `travel_time_matrix` ignores stored preferences
entirely while its siblings honor them.

**Error recovery.** Mostly excellent (see above), with three weak spots:
per-row batch errors are bare strings (`{"error": "no match"}`) instead of
the standard `{error, detail}` envelope; `not_found` and `no_route` name no
next action; and truncation is *irrecoverable* — `omitted_count: 12` with no
way to ever see those rows.

**Known schema bug.** `from_to`'s published schema (the `FromToArguments`
monkeypatch, `server.py:4300–4334`) silently drops `include_path`,
`include_elevation`, and `prefer` even though the function accepts them. An
agent reading the schema cannot discover three real capabilities.

---

## 2. Market position (August 2026)

| | Google (Grounding Lite) | Mapbox MCP | OSM community (osmmcp etc.) | TomTom / HERE / Stadia / MapQuest | **PlaceRoot** |
|---|---|---|---|---|---|
| Keyless | ✗ (GCP project + billing) | ✗ (token) | ✓ (public instances) | ✗ (keys; MapQuest pooled quota) | **✓** |
| Routing | distance/duration only | ✓ + traffic | ✓ (OSRM/Valhalla) | ✓ | ✓ free-flow |
| Isochrones | ✗ | ✓ | Valhalla only | TomTom, Stadia | **✓** |
| Batch tools | ✗ | matrix only | ✗ | ✗ | **✓** |
| Stable IDs | Place ID | Mapbox ID | ✗ | FSQ/TomTom IDs | **✓ GERS (open)** |
| Confidence / provenance | opaque AI summaries | ✗ | ✗ | ✗ (Placematic gestures) | **✓** |
| Pagination | ✗ | ✗ | ✗ | ✗ | ✗ *(nobody — open lane)* |
| outputSchema | ✗ | **✓ (almost every tool)** | ✗ | partial | **✓ (all 42 tools, #403)** |
| Traffic / hours / ratings | ✓ | ✓ | partial | ✓ | ✗ (honest non-goal) |
| Scale ceiling | quota + billing | rate limits | 1 req/s Nominatim etc. | quotas | **local cache, none** |

Three market facts shape the priorities:

1. **The field is fragmenting into "thin wrapper" vs "agent-native."**
   Sparkgeo counts 77+ geospatial MCP servers, mostly thin REST wrappers;
   the reference Google server was archived while wrapping a deprecated API.
   Google's official answer is deliberate minimalism (3 tools); Mapbox's is
   maximal surface + `outputSchema` + elicitation. Nobody occupies
   "complete *and* token-lean *and* keyless."
2. **The complaints are uniform**: schema token bloat (Stadia's own README:
   "tool schemas are the vast majority of the context cost"), errors without
   contracts, key/billing friction, response-size walls, too many
   round-trips. PlaceRoot already answers four of five better than anyone —
   it just doesn't finish the job or say so loudly.
3. **The unclaimed capabilities are exactly PlaceRoot-shaped**: pagination,
   structured confidence/provenance, batch operations, and
   reachability-filtered search are served well by *no one*, and every one
   of them is easier on a pinned static release than on a live API (results
   are reproducible, so cursors and provenance are cheap and honest).

---

## 3. The five workflows, before and after

Counts are minimum calls for a competent agent that already knows the tool
surface; "+1" marks a common extra hop (category-slug lookup, ambiguity
retry). "After" assumes the Top-5 features in §4.

### A. Local recommendations — "best cafés near Alamo Square"

| | Calls | Trace |
|---|---|---|
| Today | **1–2** | `find_near("cafe", "Alamo Square, SF")` — already one hop; +1 `search_categories` when the slug guess misses |
| After | **1** | same, with inline category synonyms already handled and a `cursor` if 10 results aren't enough |

Already best-in-class (Mapbox needs geocode → category_search). No new work
beyond pagination. "Best" ranking is confidence + distance; that's honest —
no ratings exist in open data, and the trust note says so.

### B. Trip / itinerary planning — "plan my Saturday in Lisbon: coffee, two sights, lunch, viewpoint"

| | Calls | Trace |
|---|---|---|
| Today | **7–9** | `geocode("Lisbon")` → `find_places` ×4 (one per category, each needing the geocoded point) → `optimize_route` (re-typing 5 sets of coordinates) → `route` per leg if paths wanted → `render_map` |
| After | **3** | `find_places(where="Lisbon", categories=["cafe","tourist_attraction","restaurant","viewpoint"], group_by_category=true)` → `optimize_route(stops=[ids…])` → `render_map` |

Wins come from multi-category search (one scan, grouped answer), LocationRef
(`where` takes the city name; `stops` take GERS ids from the previous
answer), and `optimize_route` already returning per-leg data + export.

### C. Delivery / multi-stop routing — "best order for these 6 addresses, driving"

| | Calls | Trace |
|---|---|---|
| Today | **2** | `geocode_batch([6 addresses])` → `optimize_route(stops=[{lat,lon}…])` |
| After | **1** | `optimize_route(stops=["addr1", "addr2", …], mode="drive")` — resolution inside the tool, per-stop `resolved` echo in the answer |

Halving an already-short chain matters because this workflow runs *n* times
a day in dispatch-style agents. Per-stop resolution failures return indexed,
self-correcting errors (`stops[3]: ambiguous — candidates: [...]`) instead
of failing the whole call.

### D. Real-estate / area analysis — "compare Noe Valley vs Bernal Heights for a family; verify this listing's claims"

| | Calls | Trace |
|---|---|---|
| Today | **4–5** | `geocode` ×2 → `compare_areas([{lat,lon},{lat,lon}], priorities=…)` → `geocode_address(listing)` → `verify_claims` |
| After | **2** | `compare_areas(areas=["Noe Valley, SF", "Bernal Heights, SF"], priorities=…)` → `verify_claims(where="301 Precita Ave…", claims=…)` |

Bonus: comparing *named divisions* by their real polygons (which
`find_places` can already do via `area`) rather than radius circles makes
the comparison boundary-accurate — a correctness win, not just a hop win.

### E. Reachability — "which supermarkets can I reach within 15 minutes' walk of my hotel?"

| | Calls | Trace |
|---|---|---|
| Today | **3, and wrong** | `geocode(hotel)` → `isochrone(lat, lon, 15, "walk")` → `find_places(radius_m=1200)` — the radius is a *guess at* the isochrone; the polygon can't be used as a search filter, so results include unreachable places across a river and miss reachable ones |
| After | **1, and right** | `find_places(where="Hotel Mundial, Lisbon", within={"minutes": 15, "mode": "walk"}, category="supermarket")` — reachability filter evaluated against the actual street graph |

This is the marquee feature: **no MCP server on the market can do
reachability-filtered search in any number of calls** without the agent
doing point-in-polygon math itself. PlaceRoot has both halves (graph +
places) in one process already; `suggest_areas` proves the shed-intersection
machinery exists.

---

## 4. Roadmap

### Top 5 features

Ordered by (impact on round-trips + tokens + dead ends) ÷ effort.

#### 1. `where` / LocationRef — one location argument, everywhere *(impact: ████▊ · effort: medium)*

Every tool that takes a location accepts one union type: `{lat, lon}` |
GERS id string | free-text name string. List parameters (`stops`, `origins`,
`areas`, `anchors`, …) accept lists of the same union. Resolution happens
in-process with the same alt-name/fuzzy tiers `find_places` already has;
every answer echoes a compact `resolved` block so the agent learns the
canonical id for later turns, and ambiguity returns the existing
`ambiguous_place` candidates envelope *per item, indexed*.

Why first: it deletes the single most common round-trip in every workflow
(§3: −1 to −4 calls each), removes coordinate re-typing (an error class as
well as a token cost), and *unifies the five point conventions without
breaking anyone* — `{lat,lon}` inputs keep working; the change is purely
additive per tool. Roll out in dependency order: `optimize_route`,
`compare_areas`, `isochrone`, `summarize_area`, `route`, then the rest;
`from_to` and `find_near` fold into `route` and `find_places` as
deprecated aliases at the end, shrinking the tool count.

#### 2. Reachability filter: `within` on `find_places` *(impact: ████▋ · effort: medium)*

`within: {minutes, mode, of?}` filters any place search to the street-graph
walkshed/bikeshed/driveshed. Defaults `of` to `where`. Reuses the
`suggest_areas` shed machinery and the same `confirm`/`needs_confirm` gate
for cold graphs. Turns workflow E from 3-calls-and-wrong to 1-call-and-right,
makes `suggest_areas`, `neighborhood_verdict` and `meeting_point` sharper
internally, and is the capability to lead every comparison table with:
*keyless, global, exact reachability search — nobody else has it at any
price*.

#### 3. Structured schemas: `outputSchema`, real enums, visible defaults *(impact: ████ · effort: low)* — **done**

Three fixes, shipped across two PR series:
- ~~Move enum values out of prose into JSON Schema `enum`s...~~ and
  ~~make defaults visible and uniform...~~ and the `from_to` bug fix
  (republish its dropped `include_path`/`include_elevation`/`prefer`) —
  all shipped in #396.
- Declare `outputSchema` on all 42 tools (#403). The response shapes were
  already stable and documented in prose; this makes them machine-checkable,
  closes the one MCP-conformance gap the repo's own benchmarks conceded to
  Mapbox (docs/benchmarks-vs.md), and lets typed-agent frameworks
  (increasingly the default) bind results without guessing. Hand-authored
  rather than derived (every tool returns a bare, heterogeneous `dict`):
  a first wave of 12 tools gets precise per-field schemas, the rest get a
  generic honest envelope (`anyOf[success, error]`, both
  `additionalProperties: true`) — see §5.3 below for the pattern and
  tests/test_output_schemas.py for the drift-proofing.

Net schema tokens: roughly flat for `inputSchema`+description (enums offset
prose from #396), plus an honest `outputSchema` cost (#403) — which the
listing cache hints already amortize to once per day per client. See
docs/benchmarks.md's generated section for the actual outputSchema token
total, broken out from inputSchema.

#### 4. Pagination cursors *(impact: ███▊ · effort: low)*

Any truncated list answer carries `cursor: "<opaque>"` beside the existing
`truncated`/`omitted_count`; list tools accept `cursor` to continue.
Because answers come from a **pinned, immutable release**, a cursor is just
`(query-hash, release, offset)` — perfectly consistent, no server state, no
expiry within a release, and a one-line honesty note when a cursor from an
older release is replayed. No other geospatial MCP server paginates
*anything*; for PlaceRoot it's nearly free, and it converts the flagship
tool's biggest weakness (irrecoverable truncation under the token budget)
into a strength: *small answers by default, complete answers on demand.*

#### 5. `find_places` answer diet: `detail` tiers + multi-category *(impact: ███▌ · effort: low)*

- `detail: "ids" | "compact" | "full"` (default `compact`). `compact` trims
  the current 12-field row to name, category, id, distance_m, and a
  1-character trust glyph expanded in a single legend line — targeting
  ~250–350 tokens for 10 results instead of 802. `full` is today's row;
  `ids` enables cheap chaining into `resolve_place_batch`.
- `categories: [...]` (up to 5) with `group_by_category`, one scan —
  itinerary and errand workflows stop paying one call per category.

This directly fixes the one benchmark number quoted against PlaceRoot
(811 vs Mapbox's 117 for "nearest coffee") without giving up multi-result
answers.

### Next tier (do after, or opportunistically)

- **Batch error envelope unification** — per-row errors adopt the standard
  `{error, detail}` shape; `not_found`/`no_route` gain a `try` hint. Tiny
  effort; finishes the self-correction story.
- **MCP roots as geofence** — when the client publishes roots containing a
  geography (workspace config, or an explicit `placeroot.json`), auto-warm
  that region at startup and bias `resolve_place`/`geocode` ranking to it.
  Kills the "did you mean Springfield, which one" class of ambiguity for
  the 90% of installs that live in one metro. Design carefully: a *bias*,
  never a hard filter, and always disclosed in the answer.
- **Elicitation + sampling adoption** — where the client advertises
  elicitation, `needs_confirm` and `ambiguous_*` upgrade from
  error-shaped round-trips to native prompts (the hand-rolled protocol
  stays as fallback); `verify_claims` optionally uses sampling to
  decompose free-text listings server-side, deleting the client-side
  parsing step its docstring currently delegates.
- **Geometry set ops** — implement `union`/`intersect`/`difference` in
  `geometry_op` (today's only "not implemented"); Mapbox ships these as
  Turf tools and agents do use them.
- **`plan_area_visit` prompt, not tool** — the itinerary workflow (§3B) as
  a seventh workflow prompt; prompts cost zero listing tokens. **Done
  (#405).**
- **Transit (exploratory, flag-gated)** — the largest honest capability gap
  vs. keyed servers. GTFS feeds are open and keyless, so it *fits the
  charter*, but it's a new data pipeline with freshness obligations; ship
  behind `PLACEROOT_TRANSIT=<feed-dir>` for self-hosters first, and only
  promote if maintenance cost proves bounded. Never fake it with straight
  lines.

### Explicit non-goals (unchanged, and worth restating in marketing)

Live traffic, opening hours, ratings, photos: open data doesn't carry them;
PlaceRoot says so instead of pretending. Hazard/property-risk scoring stays
permanently out of scope per CONTRIBUTING.md.

---

## 5. Revised tool schemas (highest-impact changes)

### 5.1 Shared `$defs` (referenced by every revised tool)

```json
{
  "$defs": {
    "LocationRef": {
      "description": "A place, three ways: coordinates, a GERS id, or a free-text name (resolved with the same alt-spelling/fuzzy tiers as find_places; ambiguity returns an ambiguous_place error with candidates).",
      "oneOf": [
        {
          "type": "object",
          "properties": {
            "lat": { "type": "number", "minimum": -90, "maximum": 90 },
            "lon": { "type": "number", "minimum": -180, "maximum": 180 }
          },
          "required": ["lat", "lon"],
          "additionalProperties": false
        },
        {
          "type": "string",
          "description": "GERS id (e.g. \"08f2830...\") or free-text name (e.g. \"Alamo Square, SF\")"
        }
      ]
    },
    "Mode": {
      "type": "string",
      "enum": ["walk", "cycle", "drive"],
      "description": "Travel mode. Default: the stored preference (see placeroot://preferences), else walk."
    },
    "Within": {
      "type": "object",
      "description": "Reachability filter: keep only results inside the street-graph area reachable in `minutes` by `mode` from `of` (default: the tool's main location). Cold graphs return needs_confirm with an ETA; retry with confirm=true.",
      "properties": {
        "minutes": { "type": "number", "exclusiveMinimum": 0, "maximum": 60 },
        "mode": { "$ref": "#/$defs/Mode" },
        "of": { "$ref": "#/$defs/LocationRef" }
      },
      "required": ["minutes"],
      "additionalProperties": false
    }
  }
}
```

### 5.2 `find_places` v2 (input)

```json
{
  "name": "find_places",
  "description": "Named places near a location or inside a named area, nearest first. Filter by category slug(s) (unknown text → search_categories), brand, confidence, status. `within` filters to true street-graph reachability. Truncated answers carry `cursor`; pass it back to continue.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "where":       { "$ref": "#/$defs/LocationRef" },
      "radius_m":    { "type": "number", "default": 1000, "maximum": 25000 },
      "within":      { "$ref": "#/$defs/Within" },
      "categories":  { "type": "array", "items": { "type": "string" }, "maxItems": 5,
                       "description": "Overture category slugs; one scan, results grouped when group_by_category=true" },
      "group_by_category": { "type": "boolean", "default": false },
      "name":        { "type": "string" },
      "brand":       { "type": "string" },
      "min_confidence": { "type": "number", "minimum": 0, "maximum": 1 },
      "operating_status": { "type": "string", "enum": ["open", "closed", "unknown"] },
      "has_website": { "type": "boolean" },
      "has_phone":   { "type": "boolean" },
      "detail":      { "type": "string", "enum": ["ids", "compact", "full"], "default": "compact",
                       "description": "compact ≈ 30 tokens/row; full adds contacts/brand/status fields; ids for chaining into resolve_place_batch" },
      "limit":       { "type": "integer", "default": 10, "maximum": 25 },
      "cursor":      { "type": "string", "description": "Continuation cursor from a previous truncated answer. Valid for the same query on the same data release." }
    },
    "required": ["where"],
    "additionalProperties": false
  }
}
```

Replaces today's 13 parameters across three mutually-exclusive addressing
modes (`lat`+`lon` vs `area` vs `division_id` — all now `where`) and absorbs
`find_near` (`find_near(category, near)` ≡ `find_places(where=near,
categories=[category])`).

### 5.3 `find_places` v2 (outputSchema — the paginated-list envelope, reused by every list tool)

```json
{
  "outputSchema": {
    "type": "object",
    "properties": {
      "resolved": {
        "type": "object",
        "description": "How `where` was resolved — canonical id for reuse in later calls.",
        "properties": {
          "id":   { "type": ["string", "null"] },
          "name": { "type": "string" },
          "lat":  { "type": "number" },
          "lon":  { "type": "number" },
          "matched_by": { "type": "string", "enum": ["exact", "alt_name", "fuzzy", "coordinates", "gers_id"] }
        }
      },
      "results": {
        "type": "array",
        "items": {
          "type": "object",
          "properties": {
            "id":         { "type": "string", "description": "Stable GERS id" },
            "name":       { "type": ["string", "null"] },
            "category":   { "type": "string" },
            "distance_m": { "type": "number" },
            "lat":        { "type": "number" },
            "lon":        { "type": "number" },
            "confidence": { "type": "number", "minimum": 0, "maximum": 1 },
            "trust":      { "type": "string", "enum": ["strong", "ok", "weak", "unknown"],
                            "description": "Structured tier behind the prose trust_note; full detail via place_details" }
          },
          "required": ["id", "category", "lat", "lon"]
        }
      },
      "truncated":     { "type": "boolean" },
      "omitted_count": { "type": "integer" },
      "cursor":        { "type": ["string", "null"],
                         "description": "Present iff truncated; pass back to continue. Stable within a data release." },
      "note":          { "type": "string" }
    },
    "required": ["results"]
  }
}
```

Note `trust` as a structured enum *beside* the prose `trust_note` (kept in
`detail: "full"`): agents can branch on it without string-matching prose.

### 5.4 `optimize_route` v2 (input)

```json
{
  "name": "optimize_route",
  "inputSchema": {
    "type": "object",
    "properties": {
      "stops": {
        "type": "array",
        "items": { "$ref": "#/$defs/LocationRef" },
        "minItems": 2, "maxItems": 10,
        "description": "Coordinates, GERS ids, names, or street addresses, mixed freely. Per-stop resolution failures return indexed errors (stops[3]: ...) with candidates — the whole call is not failed."
      },
      "mode":        { "$ref": "#/$defs/Mode" },
      "roundtrip":   { "type": "boolean", "default": true },
      "start_index": { "type": "integer", "default": 0 },
      "confirm":     { "type": "boolean", "default": false }
    },
    "required": ["stops"],
    "additionalProperties": false
  }
}
```

### 5.5 `route` v2 (input; absorbs `from_to`, fixes the dropped-params bug)

```json
{
  "name": "route",
  "inputSchema": {
    "type": "object",
    "properties": {
      "from":              { "$ref": "#/$defs/LocationRef" },
      "to":                { "$ref": "#/$defs/LocationRef" },
      "mode":              { "$ref": "#/$defs/Mode" },
      "include_path":      { "type": "boolean", "default": false },
      "include_elevation": { "type": "boolean", "default": false },
      "prefer":            { "type": "string", "enum": ["flat"] },
      "confirm":           { "type": "boolean", "default": false }
    },
    "required": ["from", "to"],
    "additionalProperties": false
  }
}
```

Migration for all of the above: v2 parameters land additively beside the old
ones (`lat`/`lon` continue to work as `where: {lat, lon}` internally); old
spellings leave the *published schema* one minor version later and keep
being *accepted* silently for a deprecation window, so existing agents and
transcripts never break. The §5 schemas above are the *post-deprecation*
end state: while any legacy spelling is still accepted, the published
schemas must keep `additionalProperties: true` and leave `where` optional
(requiring one-of `where` | legacy params in prose), or a client that
validates against the published schema would reject calls the server still
honors. Response-shape additions (`resolved`, `cursor`,
`trust`) are additive and therefore non-breaking under the project's own
semver reading.

---

## 6. Example agent conversations, before → after

### "Which supermarkets are within 15 minutes' walk of Hotel Mundial in Lisbon?"

**Before — 3 calls, ~1,400 tokens of tool traffic, and the answer is wrong at the edges:**

```
→ geocode("Hotel Mundial, Lisbon")                        (~90 tok)
← {results: [{name: "Hotel Mundial", lat: 38.714, lon: -9.136, ...}]}
→ isochrone(lat=38.714, lon=-9.136, minutes=15, mode="walk")   (~540 tok)
← {polygon: ..., area_km2: 2.9}          # agent can't use this as a filter
→ find_places(lat=38.714, lon=-9.136, radius_m=1200,
              category="supermarket")                     (~800 tok)
← 10 rows      # radius circle ≠ walkshed: includes across-the-river rows
```

**After — 1 call, ~350 tokens, graph-exact:**

```
→ find_places(where="Hotel Mundial, Lisbon",
              within={minutes: 15, mode: "walk"},
              categories=["supermarket"])
← {resolved: {name: "Hotel Mundial", id: "08f28...", matched_by: "exact"},
   results: [{id, name, category, distance_m, walk_minutes, trust}, ×8],
   note: "reachability filtered against the street graph; 3 in-radius
          places excluded as unreachable in 15 min"}
```

### "Best order to hit these 6 addresses by bike, starting from the depot"

**Before — 2 calls, coordinates re-typed through context:**

```
→ geocode_batch(["Depot, Rua X 1", "Rua A 12", ... ×6])   (~350 tok)
← 6 coordinate pairs
→ optimize_route(stops=[{lat,lon} ×6], mode="cycle", roundtrip=true)
← {order, legs, total, export}                            (~600 tok)
```

**After — 1 call:**

```
→ optimize_route(stops=["Depot, Rua X 1, Lisboa", "Rua A 12, Lisboa", ...],
                 mode="cycle")
← {resolved: [{stop: 0, name: ..., id: ...}, ...],
   order: [0, 3, 1, 5, 2, 4], legs: [...], total: {...}, export: {...}}
```

And when stop 4 is ambiguous, the agent no longer restarts the chain — it
gets `{"error": "ambiguous_place", "index": 4, "candidates": [...]}` and
retries one argument.

### "Compare Noe Valley and Bernal Heights for a family with a dog"

**Before — 3 calls:** `geocode` ×2 → `compare_areas([{lat,lon}, {lat,lon}], priorities=[...])`, on radius circles around the centroids.
**After — 1 call:** `compare_areas(areas=["Noe Valley, San Francisco", "Bernal Heights, San Francisco"], priorities=[...])`, on the divisions' real polygons.

Across the five §3 workflows: **17–22 calls today → 8 calls** for the same
answers, with the two most common answers also 2–3× cheaper in tokens.

---

## 7. Positioning — "keyless, open-data, agent-optimized"

**One-line claim:** *The only maps server your agent can use without asking
anyone's permission — and the only one that shows its work.*

Three pillars, each defensible because competitors structurally can't follow:

1. **Keyless is a capability, not a discount.** Every keyed competitor makes
   the agent's operator create accounts, wire billing, and absorb rate
   limits — Google needs a GCP project; public-OSM servers throttle at
   1 req/s. PlaceRoot's local cache means an agent can hammer it in a loop
   for free, offline after warmup, self-hosted end-to-end. Frame it as
   *agent-economics safety*: no surprise invoice, no 429 mid-workflow, no
   quota shared with production. A keyed vendor cannot copy this without
   destroying their pricing model.

2. **Open data, honestly served.** Lean into the trust machinery rather than
   hiding the data's limits: structured `confidence` and `trust` tiers,
   `data_version` on every install, `changes_in_area` framing
   disappearances honestly, an explicit "what it deliberately does not do"
   list. The market's provenance story is empty (one small vendor gestures
   at it); Google's is *negative* (opaque AI-generated summaries).
   Positioning line: *when your agent tells a user "there's a pharmacy 400 m
   away," PlaceRoot is the only server that also tells the agent how much to
   trust that.*

3. **Agent-optimized means round-trips and tokens, provably.** Keep doing
   the thing no vendor does: publish the benchmark harness, including the
   rows we lose, and fix the losing rows (features 3–5) instead of
   reframing them. Concrete claims to own once the Top-5 ship: *"one call,
   one location argument, everywhere" · "the only paginating geo server" ·
   "the only reachability-filtered search at any price" · "24k → pick your
   surface: 866-token progressive mode."* The 42-tool surface is a
   liability in the default install and a strength in `PLACEROOT_TOOLS`
   terms — make `core` or `progressive` the *recommended* install in the
   README quick start, and let `all` be the power option.

**Who it's for, sharpened:** agents that run *many* spatial queries —
research loops, dispatch, area analysis, itinerary builders — where per-call
pricing and rate limits make keyed APIs structurally wrong, and where honest
confidence beats fresher-but-opaque data. When the question needs live
traffic, opening hours, or ratings, say "pair us with a commercial API for
that leg" out loud; the candor is the brand.

---

## Appendix: sequencing sketch

| Quarter | Ships |
|---|---|
| Now → +1 release | Feature 3 (outputSchema, enums, defaults, `from_to` bug), feature 4 (cursors), batch-error envelope — all additive, low-risk |
| +2 | Feature 5 (`detail` tiers, multi-category), re-capture benchmarks, README repositioning (recommend `core`/`progressive`) |
| +3 | Feature 1 (LocationRef rollout across tools), deprecation notes for `find_near`/`from_to` |
| +4 | Feature 2 (`within` reachability filter) + marquee benchmark vs. the field; MCP roots geofence; elicitation upgrade |
| Exploratory | Transit behind `PLACEROOT_TRANSIT`, geometry set ops, sampling in `verify_claims` |
