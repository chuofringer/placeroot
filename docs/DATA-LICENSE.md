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

## The optional recreation layer

`PLACEROOT_RECREATION_LAYER` (see [RECREATION.md](RECREATION.md)) makes the
places tools additionally read Overture's `base` theme — `type=land_use` and
`type=land` — for playgrounds, parks, dog parks, nature reserves and beaches.

There is nothing new to accept here. It is the same Overture release, read
the same way, under the terms already listed in the table above: the `base`
theme is substantially OSM-derived, so **ODbL 1.0** applies to those rows
exactly as it does to `land_use_at` and `infrastructure_at` results today.
Rows carry Overture's own per-feature `sources` array and real GERS ids, so a
mixed result set stays separable by license.

Because nothing is extracted, copied or redistributed — the layer is a query,
not a dataset — the ODbL share-alike question a locally built OSM extract
would raise does not arise.

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
- `benchmarks/competitors/` — snapshots of other projects' MIT-licensed
  output plus vendors' published documentation examples, kept for a
  reproducible benchmark; see
  [`benchmarks/competitors/README.md`](../benchmarks/competitors/README.md)
  for provenance and reasoning.
