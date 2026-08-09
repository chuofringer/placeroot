# Benchmarks

Three scripts live here, measuring different things:

- **`token_benchmark.py`** (issue #26) — accuracy plus tokens-per-correct-
  answer against **live** Overture data, versus the raw payload an agent
  would have had to read itself. Needs network. Writes `results.md`. The
  rest of this file describes it.
- **`token_efficiency.py`** (issue #178) — fully **offline**, on committed
  fixtures: the MCP schema surface of every registered tool (the context an
  agent pays before asking anything) plus per-answer response cost for a
  fixed scenario suite. Writes the generated section of
  [`../docs/benchmarks.md`](../docs/benchmarks.md), which carries its
  methodology and analysis.
- **`competitor_comparison.py`** (issue #208) — the same two numbers, but
  head-to-head against **Mapbox MCP** and the archived Anthropic **Google
  Maps** reference server. Also fully offline: their tool lists and their
  answers are vendored snapshots under [`competitors/`](competitors/), with
  repo, commit and capture date in
  [`competitors/provenance.json`](competitors/provenance.json). Writes the
  generated section of
  [`../docs/benchmarks-vs.md`](../docs/benchmarks-vs.md), which carries the
  methodology and the honest-limitations paragraph. The network-using
  capture scripts in [`competitors/capture/`](competitors/capture/) are run
  by hand, never by CI.

# Token benchmark (issue #26)

## Reality check first

[GeoBenchX](https://github.com/Solirinai/GeoBenchX) and GeoAgentBench are
*agent-with-LLM* benchmarks: they measure whether an LLM correctly chooses
and chains geospatial tools across a conversation. Running either faithfully
needs LLM API calls, which this repo has no credentials for and won't fake.

What's here instead is something narrower, but real and runnable today:
`token_benchmark.py` runs 30 spatial questions — inspired by GeoBenchX's own
task categories (nearest-POI, area comparison, point-in-admin,
within-distance, isochrone reachability) — directly against **live**
Overture data, checks each answer programmatically, and measures **tokens
per correct answer**: placeroot's compact tool response vs. the token cost
of the equivalent *raw* payload (unprocessed rows / full geometry) an agent
would have had to fetch and read itself without placeroot's tool doing the
filtering, ranking, point-in-polygon, or graph-building.

No LLM is called anywhere in this benchmark. It measures the *data* side of
the "answers, not data dumps" design rule (README.md), not an agent's tool
choice — that's what a real GeoBenchX/GeoAgentBench integration would add.
The task format here (`Task(name, category, fn)`, where `fn()` calls the
placeroot tool(s) and returns a checked `Outcome`) is deliberately generic
enough that a future LLM-driven harness could reuse the same task
definitions and checkers, swapping in an agent's own tool calls for the
direct calls used here — that integration is tracked as future work under
this issue's lineage, not solved here.

## Running it

```bash
uv run python benchmarks/token_benchmark.py
```

Hits live Overture S3 (no fixture, no mock — same posture as
`scripts/geocode_benchmark.py`). Each task is 1-3 tool calls; with a warm
tile cache this finishes in a couple of minutes, longer cold (the isochrone
tasks build a street graph from scratch and are the slowest part). Prints
the report and overwrites `benchmarks/results.md` with the run's actual
output — every number in `results.md` comes from that run, nothing hand-
edited in.

The harness itself (task definitions, the `Outcome`/`run_task` scoring
machinery, the raw-payload comparator helpers) is also exercised offline in
`tests/test_token_benchmark.py`, against the same committed fixtures the
rest of the test suite uses — `uv run pytest` runs that as part of the
normal suite, no network required.

## The other half of the cost: the schema surface

This benchmark measures the tokens an *answer* costs. The tool schemas cost
tokens too — ~9.2k estimated tokens across all 25 tools, paid once per
conversation before the agent asks anything, which is roughly 56 median
answers' worth. That side isn't reduced by better answers; it's reduced by
registering fewer tools. See `PLACEROOT_TOOLS` in the top-level README
(issue #182) for the profiles and their measured per-profile surface —
`core` is ~50% smaller, `routing` ~76%.

## Task categories and what "raw" means per category

Every task calls placeroot's tool(s) through `placeroot.server`'s plain
Python functions directly (the same functions the MCP server exposes, see
`server.py`), not over an MCP client transport — this measures response
payload size, which doesn't depend on the transport. Both the placeroot
side and the raw side are measured with the same `len(json)//4` heuristic
`placeroot.budget.estimate_tokens` uses, so every ratio below is directly
comparable to the server's own token-budget accounting.

- **point_in_admin** (`admin_lookup`): does the containing-division chain
  include the point's real county and state? Raw side: every division
  polygon that actually contains the point, with full unsimplified GeoJSON
  geometry (`ST_AsGeoJSON`) — the full division polygon, not the tool's
  `{name, type, id}` chain entry.
- **within_distance**: is a place matching a category within N meters,
  true/false. Raw side: every place row (every column, not the curated
  id/name/category/distance the tool returns) inside the same search
  window (`max_distance_m * 2`, matching the tool's own search radius).
- **nearest_poi** (`find_places`): does a coffee_shop search near an iconic
  downtown core turn up a Starbucks in the top 3 (top-3, not top-1,
  specifically to tolerate ordinary data drift between Overture releases)?
  Raw side: every place row in the same radius/category predicate.
- **area_comparison** (`compare_areas`): does a permanently dense downtown
  core show more total places than a permanently rural/wilderness point at
  the same radius — a coarse, durable fact, not a fragile exact-count
  check. Raw side: every place row in both areas' radii, combined.
- **isochrone**: sanity checks (a well-connected urban point reaches a
  nonzero area) plus two durable physical invariants — a longer time budget
  reaches an equal-or-larger area than a shorter one, and a faster mode
  (drive/cycle) reaches an equal-or-larger area than walking in the same
  time, at the same point. Raw side: every transportation segment row
  (with WKT geometry) in the same auto-derived graph-extraction radius the
  tool itself used — i.e. the raw street-network edges an agent would have
  had to fetch and read to answer the question itself, not the tool's
  compact polygon + stats.

## Honesty rules

- A task whose checker fails is counted in the aggregate, not dropped —
  `results.md`'s table includes every task that ran, correct or not, and a
  "Failure detail" section spells out exactly what each miss got instead of
  what it expected.
- The raw-payload methodology is documented per category above rather than
  left implicit; where it undercounts the true raw payload (isochrone's raw
  segments use WKT text in place of the raw binary WKB geometry column, so
  the row stays JSON-serializable without a custom encoder), that's called
  out in the code, not smoothed over.
- No number in this file is asserted without `benchmarks/results.md`
  backing it — `results.md` is regenerated by, and only by, actually running
  the script above against live data.

## A finding from running this

The first live run of this benchmark hit a real, reproducible-at-the-time
tile-cache incompleteness: `find_places` near Times Square returned zero
results (with `PLACEROOT_CACHE` at its default, on) even though the same
query with `PLACEROOT_CACHE=off` found a restaurant 10m away. Clearing
`~/.cache/placeroot` and rerunning made it disappear — a fresh cache
answered the very first query correctly, and stayed correct on repeat
queries. That points to a one-off race in cache.py's background tile
materialization (see its module docstring: a cache miss should always fall
back to a direct upstream scan for *that* query, so this shouldn't be
possible even before the background fetch resolves) rather than a
systematic data gap, but it wasn't chased further here — flagged as a
follow-up worth its own issue, not fixed in this one. The committed
`results.md` reflects the rerun against a cleared cache.
