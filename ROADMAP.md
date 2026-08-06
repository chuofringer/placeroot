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

## v0.1 (current)

- [x] `find_places` — nearest named places with taxonomy, confidence, operating status
- [x] `summarize_area` — category mix for an area

## v0.2 — correct, resilient, useful

Correctness and independence first: the query layer has known geometry bugs, no
tests, and a hardcoded upstream release.

- [ ] Fix radius geometry: circular distance in SQL, honest counts (#1, #2, #3)
- [ ] Overture release auto-discovery with pinned fallback (#4)
- [ ] Graceful degradation when upstream is slow, down, or renamed (#5)
- [ ] Offline test suite and CI on committed fixtures (#6)
- [ ] Measured token-budget enforcement with visible truncation (#7)
- [ ] Local row-group cache — warm queries under 500ms, works offline (#8)
- [ ] `place_details` — one place in full (#9)
- [ ] `geocode` / `reverse_geocode` built on Overture, not Nominatim (#10)
- [ ] `admin_lookup` — point → admin hierarchy (#11)
- [ ] `compare_areas`, `within_distance` (#12, #13)
- [ ] `simplify_geometry` — the payload tool (#14)
- [ ] Expose GERS ids in every tool response — stable place references, no
      competitor surfaces them (#25)

## v0.3 — capabilities nobody else has keyless

- [ ] Self-contained map artifact: any result renders as a live HTML map, no
      CDN, no tile key, no external viewer (#15)
- [ ] Own routing stack: routable graph and isochrones from Overture
      transportation, no hosted routing service on the critical path (#17)
- [ ] Buildings and transportation themes (#23)
- [ ] Hosted streamable-HTTP endpoint — sells latency, never access (#24)

## Later, demand-driven

- [ ] GERS id resolution service — free-text place references → GERS ids; the
      cheap part (returning ids we already have) moved up to v0.2 (#22, #25)
- [ ] Own mirror of the places theme, so an upstream layout change is an
      inconvenience rather than an outage (#20)
- [ ] Live layers: transit feeds, weather context

## Relationships, not dependencies

A GeoLibre bridge and upstream contributions to Overture tooling are both
worth doing and actively wanted. Neither blocks a release. If a partner ships
first, PlaceRoot still works the same day.
