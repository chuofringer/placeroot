# Token efficiency: what PlaceRoot costs an agent's context

> **Latency lives elsewhere.** This page is about *tokens*. Cold-start
> latency and answer correctness are measured by
> `benchmarks/run_query_corpus.py` against 148 real user questions in 40+
> cities, each in a fresh process with an empty cache — see
> [the corpus](../benchmarks/query_corpus.py) and the weekly **Query Corpus** workflow.
> It checks answers, not just clocks: the per-tool matrix it replaced
> reported every tool under 10 s while one landmark query took 61 s and
> returned nothing, and `geocode("Casablanca")` answered *Chile* in 0.2 s.

"Compact answers" is PlaceRoot's core design claim. This page is the number
behind it — and, just as deliberately, the number *against* it: the tool
schemas an agent has to carry before it asks anything at all.

Everything here is produced by `benchmarks/token_efficiency.py`, which runs
fully offline against the repo's committed test fixtures. Regenerate it with:

```bash
uv run python benchmarks/token_efficiency.py --write
```

That rewrites only the marked section below. The prose is hand-written; the
numbers never are.

## The two costs

An MCP server charges an agent twice, and the two costs behave differently:

1. **Schema surface** — every registered tool's name, description, and JSON
   input schema, sent once per conversation in the `tools/list` reply,
   whether or not a single tool is ever called. It scales with the *number of
   tools*, and you pay it even on a conversation that never asks a spatial
   question.
2. **Response cost** — what one answer costs. It scales with *calls made*,
   and it is the thing PlaceRoot's design actually optimizes: ranked, capped,
   pre-joined answers instead of raw Overture rows.

Reporting only the second would be marketing. Both are below.

## Methodology

- **Schema surface** is read from the live `MCPServer` registry in
  `src/placeroot/server.py` via `mcp.list_tools()`, then serialized with the
  same compact JSON the MCP SDK puts on the wire (`by_alias`, no whitespace
  padding, `None` fields omitted). It is introspected, never hand-copied, so
  adding or editing a tool moves these numbers on the next run.
- **Response cost** is the JSON of a real tool response for a fixed scenario
  suite, called through the same `placeroot.server` functions the MCP server
  exposes. Scenarios are deterministic: fixed coordinates taken from
  `tests/conftest.py` and `scripts/build_routing_fixture.py`, answered from
  the committed fixtures in `tests/fixtures/`, with the Overture release
  pinned and the tile cache off. Same inputs, same numbers, every run, no
  network.
- **Token counting**: PlaceRoot depends on `duckdb` and `mcp` and nothing
  else, and this benchmark does not add a tokenizer dependency to a server
  whose whole pitch is being small. If `tiktoken` happens to be importable
  the script uses it and says so; otherwise counts are the **chars/4
  heuristic** — the same estimator `placeroot.budget.estimate_tokens` uses to
  enforce the server's own response budget, so these numbers match the
  accounting that decides when a caller sees `truncated: true`. The active
  method is printed in the generated section below, and exact character and
  byte counts sit next to every estimate so anyone with a real tokenizer can
  re-derive them without rerunning anything.

Caveat worth stating plainly: chars/4 is an approximation, and it tends to
*under*count dense JSON, where punctuation and short keys tokenize at fewer
than four characters per token. Treat the schema figures as a floor, not a
ceiling.

## Numbers

<!-- BEGIN GENERATED: benchmarks/token_efficiency.py -->

Generated 2026-08-22 by `uv run python benchmarks/token_efficiency.py --write`.

- Token counting method: **chars/4 heuristic (no tokenizer installed; same estimator as placeroot.budget.estimate_tokens)**
- Overture release pinned for the fixture run: `2026-07-22.0`
- Tools registered: **41**
- Total schema surface: **22296 tokens** (89245 chars, 89485 bytes)
- Schema cost per tool: min 158, median 476, max 1459 tokens
- Median scenario response: **208 tokens** (range 87-811)
- Break-even: the schema surface costs about as much as **107 median answers**

### Schema surface (paid once per conversation)

| tool | description tokens | inputSchema tokens | total tokens | total chars |
|---|---:|---:|---:|---:|
| `changes_in_area` | 1161 | 212 | **1459** | 5836 |
| `find_places` | 1072 | 296 | **1448** | 5793 |
| `meeting_point` | 797 | 99 | **973** | 3893 |
| `verify_claims` | 781 | 68 | **933** | 3733 |
| `route` | 729 | 128 | **928** | 3713 |
| `geometry_op` | 559 | 221 | **848** | 3394 |
| `optimize_route` | 647 | 99 | **822** | 3291 |
| `compare_areas` | 628 | 98 | **794** | 3176 |
| `resolve_place` | 601 | 112 | **787** | 3149 |
| `places_along_route` | 522 | 173 | **759** | 3037 |
| `travel_time_matrix` | 569 | 93 | **732** | 2928 |
| `water_near` | 530 | 116 | **713** | 2855 |
| `infrastructure_at` | 494 | 118 | **682** | 2729 |
| `geocode` | 559 | 44 | **669** | 2677 |
| `geocode_address` | 407 | 108 | **574** | 2298 |
| `isochrone` | 387 | 122 | **568** | 2273 |
| `address_at` | 427 | 54 | **541** | 2164 |
| `gers_lookup` | 385 | 74 | **519** | 2079 |
| `ground_location` | 361 | 77 | **498** | 1993 |
| `render_map` | 318 | 107 | **479** | 1918 |
| `place_details` | 265 | 158 | **476** | 1906 |
| `from_to` | 307 | 69 | **434** | 1738 |
| `distance_matrix` | 246 | 78 | **380** | 1522 |
| `neighborhood_verdict` | 200 | 123 | **378** | 1512 |
| `land_use_at` | 274 | 41 | **373** | 1493 |
| `search_categories` | 266 | 46 | **370** | 1482 |
| `resolve_place_batch` | 237 | 42 | **339** | 1357 |
| `elevation_at` | 242 | 41 | **338** | 1355 |
| `warmup_city` | 175 | 109 | **335** | 1341 |
| `within_distance` | 176 | 104 | **334** | 1338 |
| `geocode_batch` | 200 | 58 | **316** | 1267 |
| `preferences` | 141 | 122 | **314** | 1258 |
| `find_near` | 175 | 74 | **304** | 1217 |
| `buildings_at` | 151 | 91 | **293** | 1175 |
| `summarize_buildings` | 154 | 58 | **264** | 1059 |
| `reverse_geocode_batch` | 142 | 48 | **247** | 990 |
| `simplify_geometry` | 129 | 58 | **241** | 964 |
| `admin_lookup` | 145 | 41 | **240** | 961 |
| `data_version` | 166 | 16 | **231** | 926 |
| `reverse_geocode` | 111 | 42 | **205** | 820 |
| `summarize_area` | 53 | 57 | **158** | 635 |
| **all 41 tools** | 15889 | 3895 | **22296** | 89245 |

### Response cost (paid per tool call, measured on committed fixtures)

| scenario | question | response tokens | response chars |
|---|---|---:|---:|
| `find_places (point, 1km)` | What named places are within 1km of the fixture center? | **802** | 3208 |
| `find_places (point, category filter)` | Which coffee shops are within 1km? | **811** | 3247 |
| `geocode` | Where is 'Brooklyn'? | **87** | 348 |
| `place_details` | Tell me about the place named 'Roastery' near the fixture center. | **147** | 588 |
| `summarize_area` | What's in this 1km area? | **113** | 453 |
| `route (walk)` | How do I walk from one grid corner to another? | **436** | 1746 |
| `isochrone (15min walk)` | How far can I walk in 15 minutes from here? | **208** | 832 |

<!-- END GENERATED -->

## What the numbers say

**Answers are small.** Most scenarios land in the low hundreds of tokens; a
`route` answer is a few dozen. That is the design working: the server does
the filtering, ranking, point-in-polygon, and graph search, and returns the
conclusion rather than the evidence. `benchmarks/token_benchmark.py` (issue
#26) measures the other half of this against *live* data — the same answers
versus the raw Overture payload an agent would otherwise have had to read
itself — and the ratios there run into the hundreds and thousands.

**The schema surface is the real cost, and it is not small.** At 30 tools it
is thousands of tokens, paid before the agent does anything. The break-even
line in the generated section is the honest framing: the schemas cost roughly
what several dozen answers cost. An agent that asks two spatial questions in
a session spends far more context on *being able to* ask than on the answers.

**Tool annotations cost about a tenth of a token budget line.** Declaring MCP
`annotations` (readOnlyHint and friends) plus a display `title` on what were
then 25 tools (#193) added **839 tokens** to the schema surface (9192 →
10031, +9.1%; ~134 chars per tool). That is the price of letting a client
know, before it prompts the user, which calls touch nothing — every tool
except `render_map` is a pure lookup, and `render_map`, which writes an HTML
file, says so. It is
paid once per conversation and it is worth it, but it is not free, and it is
one more reason the fix for schema surface is fewer *loaded* tools.

**Our schemas are description-heavy, not schema-heavy.** Roughly two-thirds of
the surface is prose descriptions, not JSON `inputSchema`. That is the opposite
of the usual failure mode (see the citation below, where the worst-measured
server is 97% `inputSchema`), and it is mostly a good sign — the bytes buy
disambiguation an agent actually uses, like `find_places`' three mutually
exclusive modes. It also means the surface is *editable*: prose can be
tightened without changing the API. `find_places` alone is over a sixth of
the whole surface.

**The fix for schema surface is not shorter tools, it is fewer *loaded*
tools**, and that has shipped: `PLACEROOT_TOOLS` selects a subset profile
(e.g. `core`) so an install that only needs geocoding and place search does
not pay for building footprints, isochrones, and map rendering. See
[docs/REFERENCE.md](REFERENCE.md#loading-fewer-tools-placeroot_tools) for the
profiles and their measured per-profile surface. Rerun this benchmark after
any change to the tool set; the break-even line is the metric to move.

## Comparison to other servers

**A fair head-to-head is not possible offline, and this page does not fake
one.** Measuring another geospatial MCP server's schema surface means running
that server; measuring its response cost for the same question means calling
a live, keyed API (Mapbox, Google) with a paid account and matching query
semantics that do not actually match — different data, different result caps,
different fields. Numbers produced that way would be a comparison of billing
tiers, not of designs. No such numbers are published here, and none should be
inferred from what is.

What can be cited is published third-party measurement of the *category*:

- [`zhang-liz/mcp-token-benchmark`](https://github.com/zhang-liz/mcp-token-benchmark)
  measures tool-definition context cost across 9 popular MCP servers and
  reports a **25× efficiency spread** between best and worst; a 5-server
  setup (notion, github, playwright, filesystem, slack) occupying **26,224
  tokens, 13.1% of a 200K context window**; and, for the worst performer,
  **97% of the cost in `inputSchema` rather than descriptions**. Its Notion
  case study cuts 17,161 tokens to 773. These figures are quoted from that
  project's README as published; they were not independently reproduced here,
  and its token-counting method may differ from this page's chars/4
  estimator, so treat any cross-comparison with PlaceRoot's total as
  order-of-magnitude only.

Against that backdrop PlaceRoot's per-tool average is at or below the low end
of the range that project describes — which is a reason to keep the tool
count honest, not a reason to relax.
