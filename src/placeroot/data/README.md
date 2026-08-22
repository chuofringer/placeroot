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
  permanent test (tests/test_search_categories_intent.py::
  test_lexicon_slugs_all_exist_in_taxonomy) that fails if a row's slug
  isn't in the taxonomy, so a bad edit can't silently rot.
- **Refreshing:** add or edit rows directly; no generator. When
  `overture_categories.csv` is refreshed to a new schema tag, re-run the
  slug-validity test — a renamed/removed slug needs its lexicon row
  updated or dropped.


## category_embeddings.bin

Static local embeddings artifact (#356) backing `search_categories`' tail
extension past the lexicon: one hashed character-n-gram/word vector per
taxonomy row (slug words + path words + `category_synonyms.csv` words),
cosine-ranked against the query and blended in only at a confidence below
every lexical band, so it fills slots the lexical tiers left empty and
never outranks a lexical hit. No model, no network, no API key — see
`src/placeroot/embeddings.py`'s module docstring for the feature scheme
and why it's stdlib-only (no numpy/torch/onnx).

- **Generator:** `scripts/build_category_embeddings.py` (`uv run python
  scripts/build_category_embeddings.py`). Deterministic: an unchanged
  taxonomy/lexicon produces a byte-identical file (features hash via
  `zlib.crc32`, not Python's randomized `hash()`).
- **Format:** custom compact binary (not `.npz` — this artifact has no
  numpy dependency to read one back): `b"PRCE1"` magic, `dim`/`n_rows`
  header, a slug table, then `n_rows * dim` int8-quantized vector
  components. See `embeddings.save_artifact`/`_load_artifact` for the
  exact byte layout.
- **Size:** ~300 KiB for ~2,117 rows at `EMBED_DIM = 128` (well under the
  package-size budget; see the PR for the measured number).
- **Refreshing:** re-run the generator whenever `overture_categories.csv`
  or `category_synonyms.csv` changes — same trigger as the lexicon's
  slug-validity test, since this artifact is built from the same two
  source files with no other input.
- **Missing/corrupt artifact:** `search_categories` degrades silently to
  lexical-only (`embeddings.embedding_similarities` returns `[]`); this
  is exercised directly in tests/test_category_embeddings.py.


## geocode-index/aliases.json

Tiny landmark → city overlay for the bundled stage-0 name index (#329).
Looked up locally so a one-word famous POI (Colosseum, Eiffel Tower, Ebisu)
does not lose to a random exact division. Hints only — every returned row
still comes from the data. Not regenerated with the index; edit in place.
