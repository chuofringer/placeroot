# Show HN draft

## Title candidates

1. Show HN: PlaceRoot – keyless MCP server for spatial questions, answers not GeoJSON dumps
2. Show HN: PlaceRoot – an MCP server that queries Overture Maps directly, no API key
3. Show HN: PlaceRoot – token-budgeted map answers for AI agents, built on Overture Maps
4. Show HN: PlaceRoot – I built a keyless Overture Maps MCP server so agents stop choking on GeoJSON

(Recommendation: #1 — it names the mechanism (keyless), the substrate isn't
in the title so it doesn't read as an Overture ad, and "answers not GeoJSON
dumps" is the concrete differentiator, not a slogan.)

## First comment (post immediately after submitting)

Author here. PlaceRoot is an MCP server that answers spatial questions —
"what's near this point," "geocode this name," "what's the admin hierarchy
here" — by querying Overture Maps' public GeoParquet release directly with
DuckDB. No API key, no signup, no ETL step.

What I built this to fix: map tooling for agents is either a paid, keyed API,
or a thin OpenStreetMap wrapper handing back raw GeoJSON at whatever size the
query produces. Neither treats "fits in a context window" as a constraint.
Every PlaceRoot response is built to a ~2K-token budget and truncates
explicitly (`truncated: true`, `omitted_count`). Measured: 10 ranked coffee
shops via `find_places` costs 501 tokens against ~45,000 for the raw GeoJSON
equivalent; `summarize_area` over 1,944 places is ~320 tokens.

Geocoding is built on Overture's `divisions`/`addresses` data, not Nominatim
— deterministic matching, hit@1 100% measured over 113 live queries (a saturated task set; a harder query set is the honest next step). A local
tile cache makes repeat queries against the same area run warm in ~21ms.

MIT licensed, `uvx placeroot` or `uv run placeroot`, standard MCP stdio (a
streamable-HTTP mode exists for self-hosting). No hosted tier yet. Routing
covers walk, cycle, and drive, with walking the most exercised. Geocode
matching is deterministic (exact/prefix/substring plus name-variant
normalization like St./Saint), not a learned model — transparent, but not
state-of-the-art on ambiguous or misspelled queries.

Repo: https://github.com/chuofringer/placeroot — interested in where the
token budgets break down on real queries, and what's missing for your use
case.
