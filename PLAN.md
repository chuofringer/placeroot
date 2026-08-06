# PlaceRoot — Product Plan

**One-liner:** An open-source MCP server that grounds AI agents in the real world using
Overture Maps / OSM open data — answering spatial questions in compact, token-efficient
form, with no API key and no vendor platform.

**Repo:** https://github.com/chuofringer/placeroot

## Positioning

| Against | They are | PlaceRoot is |
|---|---|---|
| Google Maps grounding (Gemini-native + Maps Grounding Lite hosted MCP, GA Oct–Dec 2025) | Keyed + billed ($14–25/1K prompts), no-caching/no-training terms, Google-data-only | Keyless, open data you may store, cache, and train on |
| Mapbox / TomTom / Esri / CARTO / HERE / Precisely MCP servers | Funnels into paid platforms (API keys, metered) | Open data, keyless |
| srivinod1/overture-mcp-server (the one near-clone) | Same architecture (DuckDB over Overture S3, keyless), but 4★, dormant since Mar 2026, pinned six releases stale; budgets tool *schemas*, not answers; punts geocoding/routing/display to other MCPs | Maintained, honest geometry, token-budgeted *responses*, current release |
| New Overture adjacents (thatapicompany/overture-maps-mcp, Jul 2026; soapboxbuild/overture-mcp, Jun 2026) | Keyed wrappers over hosted APIs or buildings-only slices; 0★ each; thatapicompany claims "token-efficient summaries" qualitatively, no budgets, no GERS in responses beyond soapboxbuild's buildings lookups | Keyless, full theme coverage, measured budgets, GERS everywhere |
| gis-mcp and GIS-function servers (176★, active) | Raw computation toolboxes (geometry ops, data out) | Answers, not data |
| OSM MCP servers (largest 215★, abandoned >1yr; no *active* successor over 30★ — wiseman/osm-mcp has 86★ but is equally dead since Mar 2025) | Overpass/Nominatim wrappers: rate limits, usage policies, stale data | Overture GeoParquet direct: operating status, confidence, brands |
| geowire (multi-provider gateway, launched 2026-07-18) | BYOK vendor-API aggregator; "keyless" floor is rate-limited Nominatim/OSRM (incl. a keyless OSRM isochrone); 0★ as of 2026-08-06 | Truly keyless at scale; Overture-native; own routing graph |
| Camino AI (getcamino.ai) | Pure-play "location intelligence for agents" startup with a native MCP server — but API-keyed, metered, "17x cheaper than Google Places" as the pitch | Keyless and free; open data you may store and train on |
| ORATOR (Overture's experimental knowledge-graph MCP, built by Wherobots) | Regional prototype (SF Bay Area) on proprietary Wherobots/Iceberg infra, explicitly a proof-of-concept | Keyless, self-hostable, global today |
| GeoLibre / GeoAgent (opengeos) | An app for humans / a Strands-based agent that drives GIS software; no MCP layer, no Overture focus | Plumbing for agents (and a future GeoLibre bridge) |

Market context (extensive study 2026-08-05; superseding the initial Aug 2026 scan):
~40 location MCP servers across the registries; the official MCP registry returns **zero
results for "overture"**. Nobody ships token-budgeted answers as the headline feature —
the closest is Google Grounding Lite's "AI-generated place data summaries" (keyed,
billed) and srivinod1's tool-schema "progressive disclosure" (dormant). The payload
problem is now publicly named — Mapbox's "GeoAI in 2026" post (Dec 2025) calls large
geospatial payloads MCP's biggest challenge; a Geoawesome essay (June 2026) shows a
45K-token GeoJSON collapsing to a 25-token reference — but no one ships the fix as a
product. Overture itself now markets our thesis: "50 members converge on open data to
ground AI" (July 2026), with GERS pitched as the anti-hallucination anchor and headed
toward OGC standardization. Foursquare's open places data is flowing *into* Overture
(~6M POIs merged in 2025), while FSQ's own MCP server targets its paid API and is
near-dormant. OpenAI and Anthropic have picked no maps partner — Claude's connector
directory lists only TomTom; the "default maps tool" slot in both ecosystems is open.
Category traction ceiling remains low (Mapbox official: 350★; only geo-MCP story with
real HN traction: 105 points).

Re-verification sweep 2026-08-06 (three-track: open-source registry sweep, commercial
vendor survey, claim-by-claim check): **all ten claims of the 2026-08-05 study stand.**
The registry search for "overture" is still empty; the near-clone is still dormant
(last commit 2026-02-28); nobody ships numeric token budgets, GERS ids in responses,
keyless global isochrones, or self-contained offline map artifacts. The token-payload
pain point gained a second corporate validator: HERE's "Location Reasoning" (announced
May 2026) is an entire enterprise product marketed on reducing token usage. New since
the study, all adjacent rather than head-on: thatapicompany/overture-maps-mcp
(2026-07-16, keyed wrapper over a hosted API, 0★ — first competitor to even gesture at
token efficiency), capan/isochrone (registered to the official MCP registry
**2026-08-06**, keyless own-graph isochrones — but Berlin-only, pedestrian-only, one
tool), and sparkgeo/geo-mcp-servers (2026-08-02, 36★) — a curated tracker of 77
geospatial MCP servers whose only Overture entry is the dormant near-clone. The
sparkgeo tracker is both a listing target and the best ongoing competitive radar for
this category. Discoverability hazard: "Overture" searches are polluted by an
unrelated 629★ coding-agent tool of the same name (SixHq/Overture). The `placeroot`
name remained unclaimed on PyPI, npm, and GitHub as of 2026-08-06 — re-verified, but
verification is not a reservation; only #16's publishes are.

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
- [x] Fix radius geometry and count correctness (#1, #2, #3)
- [x] Overture release auto-discovery + graceful degradation (#4, #5)
- [x] Offline test suite and CI (#6); measured token budget (#7)
- [x] Local tile cache — warm ~21ms, cold under ~5s, works offline (#8, #31)
- [x] `place_details`, `compare_areas`, `within_distance` (#9, #12, #13, #41)
- [x] `geocode` / `reverse_geocode` on Overture data (hit@1 100% live, saturated set), `admin_lookup` (#10, #11, #43, #46, #47)
- [x] `simplify_geometry` (the payload tool) (#14)
- [x] Expose GERS ids in every tool response (#25)
- [x] Register domains: placeroot.dev (canonical) + placeroot.com (redirect), done
      2026-08-05; .io intentionally skipped (#27)
- [ ] Publish PyPI + npm (#16 — P0 as of 2026-08-05: the architecture is replicable
      and the registry search is empty)

Full triage on the [project board](https://github.com/users/chuofringer/projects/2).

### Phase 2 — Distribution (weeks 8–13)

- [x] Self-contained map artifact — any result opens as a live HTML map we ship
      ourselves (#15). A GeoLibre bridge is an additional output target and a community
      relationship, not a dependency; engage opengeos early either way.
- [x] placeroot.dev landing page built (#28) — deploy awaits DNS/Pages setup (yours)
- [x] Listing entries drafted for all 8 registries (docs/launch/registry-submissions.md,
      incl. the sparkgeo/geo-mcp-servers curated tracker found 2026-08-06);
      submission itself is owner-side, gated on #16's publishes
- [x] Show HN text + community outreach notes drafted (docs/launch/); posting is owner-side
- [x] "Why agents are bad at maps" essay drafted (docs/launch/), Mapbox/Geoawesome cited
- [x] Tokens-per-correct-answer benchmark shipped and run live (#26): 90% accuracy,
      median 2,543x fewer tokens than raw payloads (benchmarks/results.md)
- [x] Flagship demo: site-selection agent entirely on open data (#17 — live-verified, ~5.9K tokens for a 17-call analysis)

**Traction bar (~week 14):** judged on signals we own (#19, defined with
collection methods and the decision rule in [docs/METRICS.md](docs/METRICS.md)) — download trend over four
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

(Reassessed 2026-08-05 against the extensive competitive study.)

1. **opengeos ships an MCP layer first** — *downgraded*. Verified: no MCP layer in
   GeoLibre or GeoAgent; their agent effort targets GIS analysis via Strands, not place
   grounding. Still engage their community; still structurally defused (#15). Watch
   GeoAgent's roadmap — one Overture tool factory would change this.
2. **Foundation labs bake maps in via one big partner** — *partially materialized, at
   Google only*: Gemini has native Maps grounding and Maps Grounding Lite exports it to
   any MCP client — but keyed, billed ($14–25/1K), rate-limited, no-caching/no-training
   terms. OpenAI (Yelp/Zillow verticals via Apps SDK) and Anthropic (TomTom connector
   only) picked no horizontal partner. Keyless open data remains the wedge; Google's
   pricing gives us a concrete cost comparison.
3. **Attention, not code, is the bottleneck** → Phase 2 checklist is scheduled work;
   half of weekly hours in weeks 8–13 go to distribution.
4. **Upstream Overture changes layout, schedule, or access** → release discovery with a
   pinned fallback (#4), a schema probe that degrades per-tool rather than failing all
   (#5), a local cache that answers offline (#8), and eventually our own mirror (#20).
5. **The near-clone revives, or someone wraps existing pieces** — *escalating on a
   weeks timescale, not months*. srivinod1's overture-mcp-server has our architecture
   (dormant, 4★), and the DuckDB `overture` community extension (geocode, place
   readers, category normalization) is one wrapper away from being an MCP server.
   Two adjacents appeared within three weeks of the 2026-08-05 study and one more
   the day after (thatapicompany 2026-07-16, capan/isochrone registered 2026-08-06);
   none is head-on yet, but each occupies a fragment of our combination. Moat is
   execution: maintenance, honest geometry, token-budgeted answers, GERS ids (#22),
   current releases — and shipping #16 before the empty "overture" registry search
   fills. Standing radar: watch sparkgeo/geo-mcp-servers for new entrants.
6. **Foursquare ships keyless agent tooling on FSQ OS Places** — license permits it;
   today their MCP server is API-keyed and near-dormant, and FSQ open data flows into
   Overture, strengthening our substrate. Monitor.
7. **Overture-official tooling subsumes third parties** — ORATOR (Wherobots) is the
   foundation's experimental MCP prototype. Treat as opportunity first: engage the
   Product Council discussion, adopt GERS, stay the keyless self-hostable option
   ORATOR is not.

## Cadence

Solo, ~6–8 hrs/week alongside day job. Phase 1 ≈ 35–45 focused hours.
