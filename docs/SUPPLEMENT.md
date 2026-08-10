# Building the supplemental places layer

**Owner's runbook.** This is a mechanism, not a default: PlaceRoot answers
from Overture alone out of the box, with no key and no ETL. Read this only
if you want the extra layer.

## Why

Overture's places theme is built substantially from business listings. That
is exactly the right provenance for "the nearest pharmacy" and exactly the
wrong one for the places a family goes on a Saturday: a playground has no
storefront, a splash pad has no phone number, a trailhead has no owner to
claim it. Those categories are present in Overture but sparse and unevenly
covered, and an agent asked "what is there to do with kids near here" gets a
list of restaurants.

The supplement fills that gap from open data, locally:

| Source | License | What it contributes |
|---|---|---|
| OpenStreetMap, via the public [Overpass API](https://overpass-api.de) | ODbL 1.0 | Playgrounds, splash pads, water parks, parks, beaches, trailheads, campgrounds, museums, zoos, aquariums, libraries |
| [IMLS Public Libraries Survey](https://www.imls.gov/research-evaluation/surveys/public-libraries-survey-pls) outlet file | US federal — public domain | Every central and branch public library in the US, with address and phone |

Both are keyless, which is the point: design rule #3 in
[CONTRIBUTING.md](../CONTRIBUTING.md) says nothing on the critical path may
depend on an API key or someone else's quota, and this layer sits *off* the
critical path anyway — it exists only if you built it.

## What you need

Nothing but the repo and a network connection. Optionally, the IMLS PLS
**outlet** CSV (`pls_fyYYYY_outlet.csv`), downloaded by hand from
<https://www.imls.gov/research-evaluation/surveys/public-libraries-survey-pls>
— the outlet file, not the administrative-entity or state-summary file: only
the outlet file has one row per visitable building.

## 1. Build

One `--bbox` per area you care about, as `minlon,minlat,maxlon,maxlat`:

```bash
# A metro area, every category.
uv run python scripts/build_supplement.py \
    --bbox -74.05,40.60,-73.85,40.85 \
    --out ~/.placeroot/supplement-nyc.parquet
```

```bash
# Several areas, a subset of categories, plus the US library file.
uv run python scripts/build_supplement.py \
    --bbox -122.55,37.70,-122.35,37.83 \
    --bbox -122.35,37.44,-122.05,37.60 \
    --categories playground,splash_pad,park,beach,trailhead \
    --imls-csv ~/Downloads/pls_fy2023_outlet.csv \
    --out ~/.placeroot/supplement-bay-area.parquet
```

Categories: `playground`, `splash_pad`, `water_park`, `park`, `museum`,
`library`, `zoo`, `aquarium`, `beach`, `trailhead`, `campground`. Default is
all of them.

**Be polite to Overpass.** It is a donated, unmetered service with no key to
buy, and this script is one request per (bbox, category) with a 3-second
gap and exponential backoff on 429/504. Don't parallelize it, and keep
boxes to metro scale — a state-sized box is a request that either times out
or costs someone else their query.

Runtime is dominated by that throttle: roughly `bboxes × categories × 3s`
plus Overpass's own work, so a single metro with all 11 categories is a
couple of minutes.

## 2. What the builder does with the rows

**Mapping.** Each OSM feature becomes a place row with an Overture category
slug, so `find_places(category=...)` works unchanged. Splash pads get
`playground` as their primary category and `water_park` as an alternate, so
either slug finds them. `basic_category` is read from the same bundled
Overture taxonomy CSV `search_categories` answers from, so the two cannot
disagree.

**Unnamed features are kept — for four categories only.** Playgrounds,
splash pads, trailheads and beaches are routinely mapped without a name, and
"there is one here" is still the answer someone wanted. Everything else
without a name is dropped as noise. Note the current limitation: `find_places`
requires a non-null name, so those rows are reachable today only through
`place_details` by id. They are stored rather than discarded because the
data is real and the filter is the thing that may change.

**Deduplication** (on by default; `--no-dedup` skips it). Every supplement
row whose normalized name (casefolded, punctuation and accents folded)
matches an Overture place within 150 m is dropped, so a library that is in
both datasets appears once. Unnamed rows are never dropped — there is
nothing to match on. The pass runs one query per fetch bbox against the live
Overture release, so it needs network access; the release it compared
against is recorded in the sidecar.

**Confidence** is a documented heuristic, not a measurement: 0.5 for OSM
rows (a volunteer edit of unknown vintage), 0.7 for IMLS rows (a surveyed
federal record). Both sit below what typical Overture places carry, so
`find_places(min_confidence=0.8)` excludes the supplement wholesale.

**IMLS specifics.** Only outlet types `CE` (central) and `BR` (branch) are
included — a bookmobile (`BS`) or books-by-mail outlet (`BM`) has a depot
coordinate, not a destination. The survey's negative sentinels (`-1`, `-3`,
`-4`) are stripped from every field rather than passed through as text. The
longitude column is spelled `LONGITUD`: PLS truncates headers to eight
characters, and reading `LONGITUDE` silently yields nothing.

**Row ids are not GERS ids.** They are `osm:<type>/<id>` and
`imls:<FSCSKEY>-<FSCS_SEQ>`. They are stable within this file and meaningless
outside it — an id from the supplement will not resolve in `gers_lookup`, and
an Overture GERS id will not resolve to a supplement row. `place_details`
accepts both, because it is querying the union.

## 3. Enable it

```bash
export PLACEROOT_PLACES_SUPPLEMENT=~/.placeroot/supplement-nyc.parquet
```

Every places tool — `find_places`, `place_details`, `summarize_area`,
`compare_areas`, `within_distance`, `places_along_route`, division-bounded
`find_places` — then answers from both datasets at once. No tool gains an
argument and no response changes shape; the extra rows simply appear, ranked
on the same distance scale.

`data_version` reports the layer while it is active: the file path, its
build date, and the per-source and per-category row counts from the sidecar
`<out>.meta.json` the builder writes next to the parquet. Keep the sidecar
alongside the file — without it the tool can only report the path.

If the file is missing or unreadable, queries **fail loudly** naming the
environment variable rather than quietly answering Overture-only. A silently
absent layer is indistinguishable from an area with no playgrounds in it,
which is the kind of silently-partial answer design rule #4 exists to
prevent.

The local tile cache is unaffected: tiles materialize Overture rows only,
and the supplement is appended after cache resolution. Turning the layer off
leaves nothing behind.

## 4. Refreshing

OSM changes continuously and the PLS ships annually. Rebuilding quarterly is
plenty for a layer whose members are playgrounds and beaches; rebuild sooner
if you have added bboxes or Overture has shipped a release that materially
improved coverage in your area (the dedup pass will then drop more rows).

Rebuilding is the same command — write to a new path and re-point the
environment variable, so a failed build never leaves you with a truncated
file in place.

## Licensing, and what you owe

The supplement is **not distributed with PlaceRoot** and never will be:
ODbL's share-alike attaches to derived databases, and shipping one in the
package would attach it to every user. You build it, you hold it.

- **OSM rows** — © OpenStreetMap contributors,
  [ODbL 1.0](https://opendatacommons.org/licenses/odbl/). Attribution on
  anything user-facing; share-alike if you redistribute a database built
  from them.
- **IMLS rows** — US federal government work, public domain. No obligation,
  though crediting the Institute of Museum and Library Services is good
  manners.

Every row names its origin in `sources[0].dataset` (`OpenStreetMap` or
`IMLS Public Libraries Survey`), so a consumer can always tell a supplement
row from an Overture one and apply the right terms. See
[docs/DATA-LICENSE.md](DATA-LICENSE.md).

## What this doesn't do

- It doesn't replace Overture for anything. It only adds rows.
- It doesn't cover the world — only the boxes you asked for.
- It doesn't carry opening hours, ratings, or photos. Neither does anything
  else in PlaceRoot, for the same reason: the open data doesn't have them.
- It doesn't mint GERS ids, and it can't. Cross-dataset identity is
  Overture's job.
