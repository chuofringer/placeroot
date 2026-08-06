# Outreach notes

Short, community-appropriate posts. Each opens with what the project does
for that specific audience and asks for feedback, not stars or upvotes.

---

## r/gis

Built an MCP server (PlaceRoot) that lets an LLM agent query Overture Maps'
public GeoParquet release directly — DuckDB, row-group pushdown on bbox, no
ETL step, no API key. It's aimed at agent tooling rather than a GIS
workflow, but the query layer (nearest-places, area summaries, admin
hierarchy lookup, geocoding built on Overture's `divisions`/`addresses`
themes instead of Nominatim) is plain DuckDB-over-Parquet and might be
useful outside the MCP context too. Geocode hit@1 is measured at 98.2% over
113 live queries against a real Overture release, with the failure modes
documented (abbreviated "St." forms don't match Overture's spelled-out
names yet). Repo: https://github.com/chuofringer/placeroot — if anyone here
has opinions on the geocoding tiebreak logic or has hit correctness issues
with Overture's divisions theme specifically, I'd like to hear about it.

## r/LocalLLaMA

Made an MCP server for grounding agents in real map data without needing an
API key — PlaceRoot queries Overture Maps' open GeoParquet release directly
with DuckDB, runs entirely local/self-hosted (`uvx placeroot`, stdio or
`--http`), and every tool response is built to a ~2K-token budget instead of
dumping raw GeoJSON. Useful if you're running local models and want them to
answer "what's near this address" or "geocode this place name" without
routing through a paid geocoding API. It's MIT licensed and has no cloud
dependency beyond the public Overture S3 bucket (there's a local cache after
first query, and it still answers from cache if the network is down). Repo:
https://github.com/chuofringer/placeroot — curious how it performs paired
with smaller local models specifically, since I've only tested it with
larger ones calling the tools.

## Overture Slack

Posting in case useful to others building on Overture: PlaceRoot is an MCP
server that answers spatial questions (nearby places, area summaries,
geocoding, admin lookup) by querying the GeoParquet release directly via
DuckDB — no ETL, bbox row-group pushdown, release auto-discovery with a
pinned fallback. Geocoding is built entirely on the `divisions` and
`addresses` themes (no Nominatim), and every place response carries its
GERS id. Measured hit@1 98.2% over 113 live geocoding queries; the two
things that mattered most were pushing match-tier/population ordering into
the SQL query before the row-limit, and "City, ST" parsing — happy to share
more detail if useful to anyone else building geocoding on GERS. Repo:
https://github.com/chuofringer/placeroot — feedback on the release-discovery
approach or schema-probe fallback especially welcome, since that's the part
most exposed to upstream changes.

## OSM community forum

Not an OSM-based tool — PlaceRoot is an MCP server built on Overture Maps'
GeoParquet release rather than Overpass/Nominatim, mentioning it here
because the design tradeoff (open data, no rate limits tied to a shared
public instance, no usage policy to navigate) came directly from watching
existing OSM MCP wrappers hit those constraints. It answers spatial
questions for AI agents — nearby places, geocoding, admin hierarchy, area
comparison — with responses sized for a model's context window rather than
raw GeoJSON. Repo: https://github.com/chuofringer/placeroot — genuinely
interested in feedback from people who know the OSM-vs-Overture data-quality
tradeoffs better than I do, especially anywhere Overture's coverage or
freshness falls short of what OSM users would expect.
