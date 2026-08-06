# PlaceRoot Roadmap

## Design rules

1. **Answers, not data.** Every tool response fits in ~2K tokens. Large results
   return a summary plus a retrievable artifact, never raw GeoJSON dumps.
2. **Open data only, keyless by default.** Overture Maps GeoParquet and
   OpenStreetMap services that permit anonymous use.
3. **Out of scope, permanently: hazard- and property-risk scoring.** PlaceRoot
   answers "what is where" questions. It will not ship flood/fire/wind risk
   scores, property risk ratings, or insurance-flavored analytics.

## v0.1 (current)

- [x] `find_places` — nearest named places with taxonomy, confidence, operating status
- [x] `summarize_area` — category mix for an area

## v0.2

- [ ] `place_details` — one place in full (addresses, websites, phones, brand, GERS id)
- [ ] `compare_areas` — side-by-side stats for 2–5 areas
- [ ] `geocode` / `reverse_geocode` — via Overture divisions + addresses
- [ ] `admin_lookup` — point → neighborhood/city/county/state hierarchy
- [ ] `simplify_geometry` — token-budgeted geometry output
- [ ] Local cache of touched row groups for fast repeat queries

## v0.3

- [ ] `within_distance` travel-time mode (Valhalla isochrones)
- [ ] Buildings and transportation themes
- [ ] GeoLibre bridge — open any result as a live map
- [ ] Hosted streamable-HTTP endpoint

## Later, demand-driven

- [ ] GERS id resolution service (match free-text place references to stable Overture ids)
- [ ] Live layers: transit feeds, weather context
- [ ] npm wrapper (`npx placeroot`) shelling to uvx
