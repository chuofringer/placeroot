# Data licensing and attribution

PlaceRoot's **code** is [MIT-licensed](../LICENSE). The **data** it queries
and returns comes from the [Overture Maps Foundation](https://overturemaps.org)
public release, and that data is not uniformly licensed: each Overture theme
carries its own license, inherited from its sources. If you store, redistribute,
or build products on answers from PlaceRoot, the obligations below are yours —
PlaceRoot is a query layer and adds no rights it doesn't have.

Authoritative source: the license notes for the specific Overture release you
query (see the `data_version` tool for which release that is) at
<https://docs.overturemaps.org/attribution/>. If this page and Overture's
disagree, Overture's is right — per-theme licensing has shifted between
releases.

## Per-theme licensing

| Theme | PlaceRoot tools that read it | License | Key obligations |
|---|---|---|---|
| `places` | `find_places`, `place_details`, `summarize_area`, `compare_areas`, `within_distance`, `places_along_route`, `resolve_place` | CDLA-Permissive-2.0 | Attribution; no share-alike. You may store, cache, and build on results. |
| `divisions` | `admin_lookup`, `geocode`, division-bounded `find_places` | Substantially OSM-derived: **ODbL 1.0** | Attribution on produced works; **share-alike on derivative databases** |
| `transportation` | `route`, `isochrone`, `optimize_route`, `places_along_route`, `distance_matrix` | Substantially OSM-derived: **ODbL 1.0** | Same as above |
| `addresses` | `address_at`, `geocode_address`, `reverse_geocode` | Per-source open licenses (government open data, OpenAddresses-style feeds) | Varies by country; generally attribution |
| `buildings` | `summarize_buildings`, `buildings_at`, `gers_lookup` | Mixed per-feature (OSM/ODbL, Esri/Microsoft ML footprints, others) | Check the `sources` array on results you redistribute |
| `base` (land use, water, infrastructure) | `land_use_at`, `water_near`, `infrastructure_at` | Substantially OSM-derived: **ODbL 1.0** | Same as `divisions` |

## The optional supplemental places layer

`PLACEROOT_PLACES_SUPPLEMENT` (see [SUPPLEMENT.md](SUPPLEMENT.md)) points the
places tools at a second, locally built dataset that is **not Overture and
not distributed with PlaceRoot**. You build it yourself with
`scripts/build_supplement.py`; nothing arrives with the package, and the
layer does not exist unless you switch it on.

| Source | Rows it contributes | License | Key obligations |
|---|---|---|---|
| OpenStreetMap (via Overpass) | Playgrounds, splash pads, water parks, parks, beaches, trailheads, campgrounds, museums, zoos, aquariums, libraries | © OpenStreetMap contributors, **ODbL 1.0** | Attribution on produced works; **share-alike on derivative databases** |
| IMLS Public Libraries Survey | US public library outlets (central and branch) | US federal government work — **public domain** | None; crediting IMLS is good practice |

Every supplement row is identifiable per row: `sources[0].dataset` is
literally `OpenStreetMap` or `IMLS Public Libraries Survey`, and row ids are
prefixed `osm:` / `imls:` (they are **not** GERS ids). So a mixed result set
can always be separated back into its licenses, and a consumer that needs to
avoid ODbL obligations can filter the OSM rows out rather than guess.

The share-alike consequence is the reason this layer is built locally rather
than shipped: a database you assemble from ODbL data is a derivative
database, and redistributing it obliges you to offer it under ODbL. That is
your call to make for your own copy — it isn't one PlaceRoot makes on every
user's behalf.

## What this means in practice

- **Displaying results** (a map, a report, an app screen) built on
  ODbL-derived themes is a *produced work*: credit
  "© OpenStreetMap contributors" with a link to
  <https://www.openstreetmap.org/copyright>, alongside
  "© Overture Maps Foundation". Maps rendered by PlaceRoot's `render_map`
  carry this attribution line already.
- **Storing and caching** results locally (as PlaceRoot's tile cache does)
  is fine under every license above.
- **Redistributing a database** you built by systematically extracting
  ODbL-derived themes (divisions, transportation, base) makes that database
  a *derivative database*: ODbL's share-alike applies, and you must offer it
  under ODbL too. Places data (CDLA-Permissive-2.0) carries no such
  obligation.
- **Training models** on the data: CDLA-Permissive-2.0 was written with
  computational use in mind and permits it with attribution. For
  ODbL-derived themes the analysis is less settled — whether a trained model
  is a derivative database is a debated question; see the OSM Foundation's
  guidance before relying on it.

## Attribution strings

For anything user-facing built on PlaceRoot answers, the safe combined line
is:

> © Overture Maps Foundation · © OpenStreetMap contributors ([ODbL](https://opendatacommons.org/licenses/odbl/))

## Bundled data in this repository

- `src/placeroot/data/overture_categories.csv` — the Overture places
  category taxonomy, from the
  [OvertureMaps/schema](https://github.com/OvertureMaps/schema) repository
  (MIT-licensed), pinned to schema tag v1.9.0. See
  [`src/placeroot/data/README.md`](../src/placeroot/data/README.md).
- `tests/fixtures/*.parquet` — small extracts of the Overture release used
  by the offline test suite; the per-theme licenses above apply to them.
  `places_supplement.parquet` is the exception: it is wholly synthetic, built
  by `scripts/build_fixture.py` from invented playgrounds and libraries in a
  fake downtown, and carries no OSM or IMLS data at all.
- `benchmarks/competitors/` — snapshots of other projects' MIT-licensed
  output plus vendors' published documentation examples, kept for a
  reproducible benchmark; see
  [`benchmarks/competitors/README.md`](../benchmarks/competitors/README.md)
  for provenance and reasoning.
