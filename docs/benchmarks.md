# Token efficiency: what PlaceRoot costs an agent's context

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

Generated 2026-08-08 by `uv run python benchmarks/token_efficiency.py --write`.

- Token counting method: **chars/4 heuristic (no tokenizer installed; same estimator as placeroot.budget.estimate_tokens)**
- Overture release pinned for the fixture run: `2026-07-22.0`
- Tools registered: **27**
- Total schema surface: **11496 tokens** (46025 chars, 46145 bytes)
- Schema cost per tool: min 158, median 339, max 1316 tokens
- Median scenario response: **131 tokens** (range 41-702)
- Break-even: the schema surface costs about as much as **88 median answers**

### Schema surface (paid once per conversation)

| tool | description tokens | inputSchema tokens | total tokens | total chars |
|---|---:|---:|---:|---:|
| `find_places` | 946 | 296 | **1316** | 5267 |
| `route` | 595 | 106 | **769** | 3076 |
| `water_near` | 530 | 116 | **713** | 2855 |
| `places_along_route` | 473 | 167 | **703** | 2813 |
| `infrastructure_at` | 494 | 118 | **682** | 2729 |
| `isochrone` | 374 | 115 | **548** | 2193 |
| `address_at` | 427 | 54 | **541** | 2164 |
| `gers_lookup` | 385 | 74 | **519** | 2079 |
| `place_details` | 259 | 158 | **469** | 1879 |
| `resolve_place` | 265 | 91 | **419** | 1676 |
| `render_map` | 243 | 85 | **381** | 1526 |
| `land_use_at` | 274 | 41 | **373** | 1493 |
| `distance_matrix` | 224 | 78 | **358** | 1434 |
| `resolve_place_batch` | 237 | 42 | **339** | 1357 |
| `geocode` | 221 | 44 | **317** | 1271 |
| `search_categories` | 201 | 46 | **305** | 1220 |
| `buildings_at` | 151 | 91 | **293** | 1175 |
| `geocode_batch` | 176 | 58 | **292** | 1171 |
| `compare_areas` | 174 | 61 | **287** | 1148 |
| `within_distance` | 128 | 104 | **286** | 1144 |
| `summarize_buildings` | 154 | 58 | **264** | 1059 |
| `reverse_geocode_batch` | 142 | 48 | **247** | 990 |
| `simplify_geometry` | 129 | 58 | **241** | 964 |
| `admin_lookup` | 145 | 41 | **240** | 961 |
| `data_version` | 166 | 16 | **231** | 926 |
| `reverse_geocode` | 111 | 42 | **205** | 820 |
| `summarize_area` | 53 | 57 | **158** | 635 |
| **all 27 tools** | 7677 | 2265 | **11496** | 46025 |

### Response cost (paid per tool call, measured on committed fixtures)

| scenario | question | response tokens | response chars |
|---|---|---:|---:|
| `find_places (point, 1km)` | What named places are within 1km of the fixture center? | **688** | 2754 |
| `find_places (point, category filter)` | Which coffee shops are within 1km? | **702** | 2811 |
| `geocode` | Where is 'Brooklyn'? | **87** | 348 |
| `place_details` | Tell me about the place named 'Roastery' near the fixture center. | **131** | 525 |
| `summarize_area` | What's in this 1km area? | **113** | 453 |
| `route (walk)` | How do I walk from one grid corner to another? | **41** | 167 |
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

**The schema surface is the real cost, and it is not small.** At 25 tools it
is thousands of tokens, paid before the agent does anything. The break-even
line in the generated section is the honest framing: the schemas cost roughly
what several dozen answers cost. An agent that asks two spatial questions in
a session spends far more context on *being able to* ask than on the answers.

**Tool annotations cost about a tenth of a token budget line.** Declaring MCP
`annotations` (readOnlyHint and friends) plus a display `title` on all 25
tools — issue #193 — added **839 tokens** to the schema surface (9192 →
10031, +9.1%; ~134 chars per tool). That is the price of letting a client
know, before it prompts the user, which calls touch nothing — 24 of the 25
are pure lookups, and `render_map`, which writes an HTML file, says so. It is
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

**Where this goes next.** The fix for schema surface is not shorter tools, it
is fewer *loaded* tools: a subset profile (e.g. a `PLACEROOT_TOOLS=core`
filter) so an install that only needs geocoding and place search does not pay
for building footprints, isochrones, and map rendering. That is deliberately
out of scope here — this page exists to establish whether the problem is real
before anyone builds for it — and is tracked as issue #182. Rerun the
benchmark after any such change; the break-even line is the metric to move.

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
