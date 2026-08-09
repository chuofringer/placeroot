# PlaceRoot Roadmap

Tracked on the [project board](https://github.com/users/chuofringer/projects/2).

## Design rules

1. **Answers, not data.** Every tool response fits in ~2K tokens. Large results
   return a summary plus a retrievable artifact, never raw GeoJSON dumps.
2. **Open data only, keyless by default.** Overture Maps GeoParquet and
   OpenStreetMap services that permit anonymous use.
3. **No hard dependency on anyone else.** Nothing on the critical path may be
   gated on another project's roadmap, another service's rate limit, or another
   team's willingness to integrate. External systems may make PlaceRoot faster
   or fresher; none may make it stop working.
4. **Out of scope, permanently: hazard- and property-risk scoring.** PlaceRoot
   answers "what is where" questions. It will not ship flood/fire/wind risk
   scores, property risk ratings, or insurance-flavored analytics.

## v0.1

- [x] `find_places` — nearest named places with taxonomy, confidence, operating status
- [x] `summarize_area` — category mix for an area

## v0.2 — correct, resilient, useful (shipped 2026-08-06)

Correctness and independence first: the query layer has known geometry bugs, no
tests, and a hardcoded upstream release.

- [x] Fix radius geometry: circular distance in SQL, honest counts (#1, #2, #3)
- [x] Overture release auto-discovery with pinned fallback (#4)
- [x] Graceful degradation when upstream is slow, down, or renamed (#5)
- [x] Offline test suite and CI on committed fixtures (#6)
- [x] Measured token-budget enforcement with visible truncation (#7)
- [x] Local row-group cache — warm queries under 500ms, works offline (#8)
- [x] `place_details` — one place in full (#9)
- [x] `geocode` / `reverse_geocode` built on Overture, not Nominatim (#10) —
      live benchmark hit@1 100% over 113 queries (saturated set), warm under 0.3s (#43, #46, #47, #53)
- [x] `admin_lookup` — point → admin hierarchy (#11)
- [x] `compare_areas`, `within_distance` (#12, #13)
- [x] `simplify_geometry` — the payload tool (#14)
- [x] Expose GERS ids in every tool response — stable place references, no
      competitor surfaces them (#25)

## v0.3 — capabilities nobody else has keyless (core shipped 2026-08-06)

- [x] Self-contained map artifact: any result renders as a live HTML map, no
      CDN, no tile key, no external viewer (#15)
- [x] Own routing stack, walking MVP: routable graph and isochrones from
      Overture transportation, no hosted routing service on the critical path
      (#18; drive/cycle, concave hulls, graph caching tracked in #36–#39)
- [x] Buildings and transportation themes (#23; transportation via the routing
      stack, buildings via summarize_buildings / buildings_at)
- [ ] Hosted streamable-HTTP endpoint — sells latency, never access (#24;
      transport code shipped — `placeroot --http` — hosting/TLS/DNS is the
      owner-side remainder)

## Later, demand-driven

- [x] GERS id resolution — `resolve_place`: free-text place references →
      GERS ids, divisions and places merged (#22, #25)
- [ ] Own mirror of the places theme (#20; tooling + switchover shipped —
      scripts/mirror_theme.py, PLACEROOT_UPSTREAM_BASE, docs/MIRROR.md —
      bucket + the 10.5 GB transfer are the owner-side remainder)
- [ ] Live layers: transit feeds, weather context

## Competitive watch (verified 2026-08-06)

A three-track market re-verification confirmed the
category position: no other project ships token-budgeted answers, GERS ids in every
response, keyless Overture geocoding, keyless global own-graph isochrones, or
self-contained offline map artifacts. The position is features, not a moat — two
adjacent projects appeared within three weeks and one the day of the check. Standing
watch items, none blocking:

- **sparkgeo/geo-mcp-servers** — curated tracker of 77 geo MCP servers; our listing
  target and the category radar. Check on each release.
- **capan/isochrone** — keyless own-graph isochrones (Berlin-only, pedestrian-only);
  the nearest conceptual neighbor to our routing stack.
- **thatapicompany/overture-maps-mcp** — keyed hosted-API wrapper claiming
  "token-efficient summaries"; first competitor gesturing at our headline.
- **Camino AI** — commercial "location intelligence for agents" startup, keyed/metered.
- **Google Maps Grounding Lite** — free while Experimental; a GA pricing decision
  ($2.80–7.00/1K published as potential) changes the cost-comparison story.
- **Overture first-party tooling** — ORATOR is still an SF-Bay prototype; any move
  toward an official Overture MCP server changes the competitive picture more
  than anything else on this list.

The empty "overture" search in the official MCP registry is the clock: #16 ships
before someone else fills it.

## Relationships, not dependencies

A GeoLibre bridge and upstream contributions to Overture tooling are both
worth doing and actively wanted. Neither blocks a release. If a partner ships
first, PlaceRoot still works the same day.
