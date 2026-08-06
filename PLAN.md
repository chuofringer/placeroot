# PlaceRoot — Product Plan

**One-liner:** An open-source MCP server that grounds AI agents in the real world using
Overture Maps / OSM open data — answering spatial questions in compact, token-efficient
form, with no API key and no vendor platform.

**Repo:** https://github.com/chuofringer/placeroot

## Positioning

| Against | They are | PlaceRoot is |
|---|---|---|
| Mapbox / CARTO / Esri MCP servers | Funnels into paid platforms (API keys) | Open data, keyless |
| gis-mcp and GIS-function servers | Raw toolboxes (92 functions, data out) | Answers, not data |
| OSM MCP servers (7+, largest 215★ and abandoned) | Overpass/Nominatim wrappers, stale data | Overture GeoParquet direct: operating status, confidence, brands |
| GeoLibre | An app for humans | Plumbing for agents (and a future GeoLibre bridge) |

Market context (researched Aug 2026): ~35 location MCP servers exist; none are
Overture-native; none treat token-budgeted answers as the design principle; category
traction ceiling is low (Mapbox official: 350★). Mapbox publicly names "large geospatial
payloads in MCP" as the unsolved problem. Overture (50 members) is the industry's bet for
grounding AI in place data. Closest-concept competitor: geowire (multi-provider API
gateway, launched 2026-07-18, 0★ at time of research).

## Design rules

1. **Answers, not data.** Every tool response fits in ~2K tokens. Big results return a
   summary + retrievable artifact, never raw GeoJSON dumps.
2. **Open data, keyless by default.** Overture S3 GeoParquet via DuckDB (no ETL, no
   database); OSM services that permit anonymous use.
3. **No hard dependency on anyone else.** Nothing on the critical path may be gated on
   another project's roadmap, another service's rate limit, or another team's willingness
   to integrate. External systems may make PlaceRoot faster or fresher; none may make it
   stop working. Consequences: geocoding is built on Overture rather than wrapping
   Nominatim; the map viewer is a self-contained artifact rather than a bridge to someone
   else's app; routing is our own graph rather than a hosted Valhalla; the upstream release
   is discovered, pinned, cached, and mirrorable.
4. **Permanent scope exclusion: hazard- and property-risk scoring.** No flood/fire/wind
   risk scores, property risk ratings, or insurance-flavored analytics — ever.

## Naming

"placeroot" chosen 2026-08-05 after availability research: PyPI, npm, GitHub repo names,
and .com/.io/.dev/.ai all unclaimed; no company, product, social account, or trademark
found under the name. Runners-up rejected: geoplinth (SEO collision with Geoplin),
groundfact (social channels exist), 15 others (registered domains or package collisions).

## Phases

### Phase 1 — MVP (in progress)

- [x] DuckDB query layer over Overture S3 (release 2026-07-22.0), bbox pushdown
- [x] `find_places` — nearest named places; taxonomy, confidence, operating_status
      (verified live: downtown Austin, 8 results ≈ 390 tokens, 5.6s cold)
- [x] `summarize_area` — category mix (1,944 places ≈ 320 tokens)
- [x] MCP server (Python SDK 2.0), `uv run placeroot`
- [ ] Fix radius geometry and count correctness (#1, #2, #3)
- [ ] Overture release auto-discovery + graceful degradation (#4, #5)
- [ ] Offline test suite and CI (#6); measured token budget (#7)
- [ ] Local row-group cache — warm under 500ms, works offline (#8)
- [ ] `place_details`, `compare_areas`, `within_distance` (#9, #12, #13)
- [ ] `geocode` / `reverse_geocode` on Overture data, `admin_lookup` (#10, #11)
- [ ] `simplify_geometry` (the payload tool) (#14)
- [ ] Claim names: register placeroot.dev/.com/.io; publish PyPI + npm (#16)

Full triage on the [project board](https://github.com/users/chuofringer/projects/2).

### Phase 2 — Distribution (weeks 8–13)

- [ ] Self-contained map artifact — any result opens as a live HTML map we ship
      ourselves (#15). A GeoLibre bridge is an additional output target and a community
      relationship, not a dependency; engage opengeos early either way.
- [ ] Listings: awesome-mcp-servers, mcpservers.org, Glama, PulseMCP, Smithery, MCP registry
- [ ] Show HN + demo video; r/gis, r/LocalLLaMA, Overture Slack, OSM community
- [ ] "Why agents are bad at maps" post naming the payload problem
- [ ] Flagship demo: site-selection agent entirely on open data

**Traction bar (~week 14):** judged on signals we own (#19) — download trend over four
weeks, distinct connecting clients, inbound issues/PRs from people we did not contact, and
named real uses. Stars are reported, not a gate: they measure other people's attention, not
whether this is worth building.

### Phase 3 — Hosted tier (months 4–7, gated on Phase 2 bar)

- Free: local server forever. Hosted $0/$29/$99: remote streamable-HTTP endpoint,
  pre-warmed indexes, monthly Overture refresh, isochrones/routing on our own graph (#17).
  The hosted tier sells latency and convenience — never access.
- Infra <$100/mo (Cloudflare Workers + R2, or single box + DuckDB).
- Expectation: $500–3K MRR year one; primary value is audience + substrate for what's next.

### Phase 4 — Demand-driven expansions

GERS id resolution service; live layers (transit, weather context — not hazard risk);
buildings/transportation themes; team features; white-label for agent platforms.

## Risks

1. **opengeos ships an MCP layer first** → engage their community now; keep Overture
   data-grounding scope distinct; merge efforts if needed. Structurally defused: nothing
   we ship waits on them (#15).
2. **Foundation labs bake maps in via one big partner** → keyless open data remains the
   wedge; category growth likely helps.
3. **Attention, not code, is the bottleneck** → Phase 2 checklist is scheduled work;
   half of weekly hours in weeks 8–13 go to distribution.
4. **Upstream Overture changes layout, schedule, or access** → release discovery with a
   pinned fallback (#4), a schema probe that degrades per-tool rather than failing all
   (#5), a local cache that answers offline (#8), and eventually our own mirror (#20).

## Cadence

Solo, ~6–8 hrs/week alongside day job. Phase 1 ≈ 35–45 focused hours.
