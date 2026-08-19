# Bundled data

## overture_categories.csv

The Overture Maps places category taxonomy, bundled so `search_categories`
can resolve a free-text query to a valid category slug without a live
`DISTINCT` scan of the places dataset.

- **Source:** https://github.com/OvertureMaps/schema — `docs/schema/concepts/by-theme/places/overture_categories.csv`
- **Pinned to:** schema tag **v1.9.0**
- **Fetched:** 2026-08-06
- **Format:** `Category code; Overture Taxonomy` — semicolon-delimited, UTF-8 with BOM; the taxonomy column is a `[a,b,c]` path from root to leaf.

### Refreshing
Overture is migrating the places categories model (the old flat `categories`
property is slated for removal ~Sept 2026 in favor of `taxonomy` /
`basic_category`). When PlaceRoot bumps the Overture release it queries, refresh
this file from the matching schema tag and update the pin + date above. Keep the
same filename and column shape, or update `search_categories`' parser to match.


## geocode-index/aliases.json

Tiny landmark → city overlay for the bundled stage-0 name index (#329).
Looked up locally so a one-word famous POI (Colosseum, Eiffel Tower, Ebisu)
does not lose to a random exact division. Hints only — every returned row
still comes from the data. Not regenerated with the index; edit in place.
