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


## category_synonyms.csv

Hand-curated lexicon (#355) backing `search_categories`' phrase-intent
fallback: when a query like "fix my cracked phone screen" shares no
substring with any slug, it's tokenized and matched against each row's
slug/path words plus its synonym words here, instead of returning nothing.

- **Format:** `slug; synonym words` — semicolon-delimited, UTF-8, one row
  per slug worth disambiguating, e.g. `mobile_phone_repair; phone screen
  cracked repair fix cell smartphone broken shattered`. Synonym words are
  free-form, space-separated; case doesn't matter (tokenized the same way
  as queries).
- **Schema pin:** every slug here must exist in the pinned
  `overture_categories.csv` above (same schema tag) — enforced by a
  permanent test (tests/test_categories.py) that fails if a row's slug
  isn't in the taxonomy, so a bad edit can't silently rot.
- **Refreshing:** add or edit rows directly; no generator. When
  `overture_categories.csv` is refreshed to a new schema tag, re-run the
  slug-validity test — a renamed/removed slug needs its lexicon row
  updated or dropped.


## geocode-index/aliases.json

Tiny landmark → city overlay for the bundled stage-0 name index (#329).
Looked up locally so a one-word famous POI (Colosseum, Eiffel Tower, Ebisu)
does not lose to a random exact division. Hints only — every returned row
still comes from the data. Not regenerated with the index; edit in place.
