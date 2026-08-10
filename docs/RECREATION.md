# The recreation layer

**On by default. No build, no download, no hosting, nothing to switch on.**

To turn it *off*:

```bash
export PLACEROOT_RECREATION_LAYER=0
```

That is the whole runbook. The variable is an opt-out — unset (or any value
other than `0`/`false`/`no`/`off`) leaves the layer on.

## Why it exists

Overture's **places** theme is substantially derived from business listings.
That is the right provenance for "the nearest pharmacy" and the wrong one for
the places a family goes on a Saturday: a playground has no storefront, a
beach has no owner to claim it, a neighbourhood park has no phone number.
Those categories exist in the places theme, but they under-count badly, and
an agent asked "what is there to do with kids near here" gets restaurants.

They are not missing from Overture, though — they are in a **different
theme**. Overture's `base` theme is a direct conflation of OpenStreetMap, not
of listings, and it carries these features as polygons. PlaceRoot already
queries that theme for `land_use_at` and `infrastructure_near` — keylessly,
over the same DuckDB + S3 GeoParquet path as everything else. This layer
points the places tools at it too.

Measured against release `2026-07-22.0` over a box covering New York City
(`-74.05,40.55` to `-73.70,40.85`):

| | `theme=places` | `theme=base` |
|---|---|---|
| playgrounds | 674 | **1,552** (`type=land_use`, `class=playground`) |
| ... with no places playground within 150 m | — | **1,013** |
| parks | 1,923 | 1,887 (`type=land_use`) |
| dog parks | 56 | 132 |
| beaches | 199 | 182 (`type=land`, `class=beach`) |

So: roughly **2.5x** the playground coverage, live, from data PlaceRoot is
already reading.

## What it costs

One extra dataset scan per places query, and it is worth being explicit that
this is not free: a cold `find_places` pays for two remote scans instead of
one. The layer's reads go through the same tile cache as everything else,
under the `base_land_use` / `base_land` keys `land_use_at` already warms, so
the second and later queries over an area are served locally.

It also widens the failure surface. A places query now depends on the base
theme being readable as well as the places theme. A base type whose *schema*
drifted is dropped and logged, and the places answer still lands (with
`data_version` naming the dropped type) — but an unreachable dataset fails
the way an unreachable places dataset already does.

An install that cares more about `find_places` latency than about
playgrounds should set `PLACEROOT_RECREATION_LAYER=0`. Then the places tools
are byte-for-byte what they were before this layer existed: one theme, one
scan.

## The layer follows the places dataset

If you point PlaceRoot at your own copy of the data — `PLACEROOT_DATA_PATH`
at a local extract, or a mirror built with `scripts/mirror_theme.py` (see
[MIRROR.md](MIRROR.md)) — and say nothing about `theme=base`, the layer
**skips itself** rather than resolving base to the live S3 release. Pinning
places to a local file is not consent to a planet-scale remote scan of a
different theme, and a query that was meant to be local and fast would
quietly stop being either.

You get one warning naming the fix. To have the layer in a pinned
deployment, mirror or extract the base theme too and set:

```bash
export PLACEROOT_DATA_PATH_BASE=/path/to/base
```

Or set `PLACEROOT_RECREATION_LAYER=0` if you don't want the layer there at
all. A deployment that pins nothing — the ordinary install — is unaffected
and reads both themes from the release.

## Which tools change

Every places tool, because the change lives in the query layer's shared
FROM-clause builder: `find_places`, `find_places_in_bbox`,
`find_places_in_area`, `summarize_area`, `compare_areas`, `within_distance`,
and `place_details` (by id, so a base-theme GERS id resolves).

Categories the layer contributes: `playground`, `dog_park`, `park`,
`nature_reserve`, `beach`. `find_places(category="playground")` reaches them
through the ordinary taxonomy path — they carry real Overture category slugs,
the same ones `search_categories` returns.

## What these rows are and are not

**They are real GERS ids.** Base-theme features are GERS-registered like any
other Overture feature, so a row composes with `gers_lookup` and
`place_details` unchanged, and `sources` carries Overture's own OSM
provenance per row.

**They carry no `confidence` and no `operating_status`.** The base theme has
neither column. A `min_confidence=` or `operating_status=` filter therefore
excludes every row from this layer. That is the filter doing what it says —
but know that those two filters silently narrow you back to places-theme
results. No confidence value is synthesised, because that would be inventing
a number.

**Most have no name.** 77% of base-theme playgrounds are unnamed, because a
playground usually isn't named at all. `find_places` normally drops rows
without a primary name; that filter is relaxed **for this layer only**, so a
nameless playground 120 m away comes back as an answer with `name: null`
rather than being silently dropped. No name is synthesised from the category
either: a fabricated "Playground" is not the place's name.

**A polygon is reported at its centre.** These are areas, not points. Each
row's position is the centre of its bounding box, so `distance_m` is the
distance to the middle of the park, not to its nearest edge — you can be
standing inside a large park that reports as 400 m away. This matches how
places rows behave (one point per feature) and is the same approximation
`buildings_at` accepts.

**`data_version` says when it's on.** The `data_version` tool and the
`placeroot://data-version` resource both carry a `recreation_layer` block
whenever the layer is active, listing the categories it contributes, so an
answer's provenance is never invisible — and its absence is how a caller
knows the layer was switched off.

## What it deliberately leaves out

`pitch` (5,513 rows in the NYC box), `track`, and the eleven golf
sub-features (`bunker`, `fairway`, `green`, `tee`, ...) are *parts of*
facilities, not destinations; unioning them in would bury the playgrounds
under football-pitch polygons. `garden` is OSM's `leisure=garden`, which is
mostly someone's front yard — 4,908 rows in Manhattan alone. Marinas,
stadiums, zoos and golf courses are excluded because they **are** businesses,
and the places theme already covers them well (23 zoos and 20 water parks in
the NYC box).

**Trailheads are a genuine gap.** Neither theme has them: `trailhead` returns
0 rows from Overture places across the whole NYC box, and the base theme
doesn't model them either. OSM has them as `highway=trailhead` nodes, which
Overture doesn't currently carry. There is no honest way to answer "trailheads
near me" from Overture today, so PlaceRoot doesn't pretend to.

**Public libraries do not need a supplement.** Overture places has 513 in the
NYC box — business-listing provenance works fine for a building with a name,
an address and opening hours. An IMLS Public Libraries Survey import was
prototyped and dropped for exactly that reason: it added a manual CSV download
to the setup for data already present.

## Why not query OpenStreetMap directly?

Two obvious routes, both measured and both rejected:

**The Overpass API.** It is a JSON HTTP API, not a queryable columnar store,
so it can't be joined into a DuckDB scan — it would have to be a blocking
HTTP call on the query path. That makes every places answer depend on a
donated, volunteer-run service with no SLA and aggressive rate limits, which
[CONTRIBUTING.md](../CONTRIBUTING.md)'s design rule #3 (nothing on the
critical path depends on someone else's quota) rules out. It also can't be
cached the way a Parquet scan can.

**Raw OSM in Parquet.** This exists publicly and needs no credentials — Meta's
Daylight distribution publishes the planet at
`s3://daylight-openstreetmap/parquet/osm_features/`. It is not usable for
interactive queries, for a concrete and checkable reason: the files are
ordered by OSM element **id**, not spatially. Every row group's
`min_lon`/`max_lon` statistics span (-180, 180), so a bounding-box predicate
prunes **nothing** and "playgrounds near me" degrades into a full scan of
155 GB of ways. Bbox pushdown is the entire reason PlaceRoot can query
Overture live, and this dataset has none of it.

Overture's base theme is the same OpenStreetMap data — already conflated,
already spatially sorted, already bbox-pruned, already GERS-registered, and
already trusted by three other PlaceRoot tools. Reading it is strictly better
than re-deriving it.

## Licensing

Nothing new to accept. This is the same Overture release, under the same
terms, as every other query PlaceRoot makes — see
[DATA-LICENSE.md](DATA-LICENSE.md). Because nothing is extracted, copied or
redistributed, the ODbL share-alike question that a locally built OSM extract
would raise never arises.
