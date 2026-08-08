"""Builds the offline test fixtures: tests/fixtures/places.parquet,
tests/fixtures/division_areas.parquet, and tests/fixtures/buildings.parquet.

places.parquet: synthetic Overture-shaped places data (struct columns
matching what overture.py expects: id, bbox, names, taxonomy,
basic_category, operating_status, confidence, addresses, websites, phones,
socials, brand, sources — the last six back place_details, issue #9).
GERS ids (issue #25) are deterministic 32-char hex strings derived from each
row's index, standing in for Overture's real GERS ids without claiming to
be one.

division_areas.parquet: a small synthetic divisions-theme type=division_area
fixture (issue #11) — a handful of nested rectangular polygons (neighborhood
< locality < county < region < country) around places.parquet's downtown
cluster, plus one unrelated polygon far away to prove point-in-polygon
actually excludes non-containing divisions. Real division_area geometry is
irregular; these are rectangles because admin_lookup only needs correct
containment semantics, not realistic shapes. Named division_areas.parquet
(not divisions.parquet) because the divisions theme also has a
type=division fixture — see scripts/build_geocode_fixture.py — read by
geocode.py instead; the two live under the same theme but are different
Overture types with different schemas.

buildings.parquet: an 8x10 grid of 80 synthetic rectangular footprints
(issue #23) around places.parquet's downtown cluster — see the "Buildings
fixture" comment further down for how areas are kept exact for tests.

All three are deterministic — same seed, same output — so they can be
regenerated and diffed. Run with:

    uv run python scripts/build_fixture.py
"""

import hashlib
import math
import random
from pathlib import Path

import duckdb

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
PLACES_FIXTURE_PATH = FIXTURES_DIR / "places.parquet"
DIVISION_AREAS_FIXTURE_PATH = FIXTURES_DIR / "division_areas.parquet"
BUILDINGS_FIXTURE_PATH = FIXTURES_DIR / "buildings.parquet"
SEED = 42
EARTH_RADIUS_M = 6371000.0
METERS_PER_DEGREE_LAT = 111_320.0

# A fake "downtown" — not a real Overture location.
CENTER_LAT = 40.700000
CENTER_LON = -73.900000
DENSE_CLUSTER_N = 200
UNCATEGORIZED_TAIL = 20  # last N of the dense cluster get basic_category = NULL

CATEGORIES = [
    "coffee_shop", "restaurant", "grocery_store", "bank",
    "pharmacy", "bar", "bakery", "gym",
]

HIGH_LAT_CENTER = (78.0, 15.0)
HIGH_LAT_N = 5


def gers_id(index: int) -> str:
    """Deterministic 32-hex-char id, GERS-shaped but synthetic."""
    return hashlib.md5(f"placeroot-fixture-{SEED}-{index}".encode()).hexdigest()


def offset_point(
    lat: float, lon: float, distance_m: float, bearing_deg: float
) -> tuple[float, float]:
    """Destination point given a start point, distance, and bearing (spherical Earth)."""
    brng = math.radians(bearing_deg)
    lat1 = math.radians(lat)
    lon1 = math.radians(lon)
    ang = distance_m / EARTH_RADIUS_M
    lat2 = math.asin(
        math.sin(lat1) * math.cos(ang) + math.cos(lat1) * math.sin(ang) * math.cos(brng)
    )
    lon2 = lon1 + math.atan2(
        math.sin(brng) * math.sin(ang) * math.cos(lat1),
        math.cos(ang) - math.sin(lat1) * math.sin(lat2),
    )
    return math.degrees(lat2), math.degrees(lon2)


def build_place_rows() -> list[tuple]:
    rng = random.Random(SEED)
    rows = []

    def add(
        name, lat, lon, category, basic_category, status, confidence, alternates=None,
        addresses=None, websites=None, phones=None, socials=None, brand=None, sources=None,
    ):
        index = len(rows)
        rows.append((
            gers_id(index),
            {"xmin": lon, "ymin": lat, "xmax": lon, "ymax": lat},
            {"primary": name},
            {"primary": category, "alternates": alternates or []},
            basic_category,
            status,
            confidence,
            addresses or [],
            websites or [],
            phones or [],
            socials or [],
            brand,
            sources or [],
        ))

    # Dense urban tile: 200 points within 400m of CENTER, uniform over the disk.
    for i in range(DENSE_CLUSTER_N):
        bearing = rng.uniform(0, 360)
        distance = 400 * math.sqrt(rng.random())
        lat, lon = offset_point(CENTER_LAT, CENTER_LON, distance, bearing)
        category = CATEGORIES[i % len(CATEGORIES)]
        uncategorized = i >= DENSE_CLUSTER_N - UNCATEGORIZED_TAIL
        name = f"Cluster Place {i:03d}" if i != 5 else "Blue Bottle Roastery"
        status = "open" if i % 11 != 0 else "closed_permanently"
        confidence = round(0.5 + 0.49 * rng.random(), 2)

        # A handful of rows get full place_details fixtures; most get none,
        # matching real data where most places have thin attribution.
        details = {}
        if i == 5:  # Blue Bottle Roastery: a fully-populated place_details row.
            details = {
                "addresses": [
                    {
                        "freeform": "123 Main St", "locality": "Metropolis",
                        "region": "NY", "postcode": "10001", "country": "US",
                    },
                ],
                "websites": ["https://bluebottleroastery.example"],
                "phones": ["+1-555-0100"],
                "socials": ["https://instagram.example/bluebottleroastery"],
                "brand": {"names": {"primary": "Blue Bottle Coffee"}},
                "sources": [{"dataset": "meta", "record_id": "meta-001"}],
            }
        elif i == 10:  # A place with more addresses than the truncation cap.
            details = {
                "addresses": [
                    {
                        "freeform": f"{n} Overflow Ave", "locality": "Metropolis",
                        "region": "NY", "postcode": "10001", "country": "US",
                    }
                    for n in range(8)
                ],
                "websites": ["https://overflow.example"],
                "sources": [{"dataset": "osm", "record_id": f"osm-{n}"} for n in range(8)],
            }

        add(
            name, lat, lon,
            category if not uncategorized else "exotic_niche",
            None if uncategorized else category,
            status, confidence,
            **details,
        )

    # Circle-vs-square: a point at 1.2x a 500m test radius, on the bbox
    # diagonal — inside the square prefilter, outside the true circle.
    lat, lon = offset_point(CENTER_LAT, CENTER_LON, 500 * 1.2, 45)
    add("Corner Test Place", lat, lon, "novelty_shop", "novelty_shop", "open", 0.80)

    # Just inside the same 500m test radius, due north — must be included.
    lat, lon = offset_point(CENTER_LAT, CENTER_LON, 500 * 0.99, 0)
    add("Edge Test Place", lat, lon, "novelty_shop", "novelty_shop", "open", 0.80)

    # Well outside any bbox used in tests.
    lat, lon = offset_point(CENTER_LAT, CENTER_LON, 5000, 200)
    add("Far Away Place", lat, lon, "restaurant", "restaurant", "open", 0.75)

    # High-latitude cluster — cos(lat) is small, checks the math stays sane.
    hlat, hlon = HIGH_LAT_CENTER
    for i in range(HIGH_LAT_N):
        bearing = rng.uniform(0, 360)
        distance = 300 * math.sqrt(rng.random())
        lat, lon = offset_point(hlat, hlon, distance, bearing)
        add(f"Arctic Place {i}", lat, lon, "bank", "bank", "open", 0.9)

    # Antimeridian cluster (issue #42): points straddling the +/-180 seam on
    # both sides, close enough together that a radius search centered on the
    # seam (e.g. lat=10.0, lon=179.99) must return rows from both sides for
    # the bbox prefilter to be correct. Distinct categories so find_places'
    # category filter and summarize_area's category mix can each be checked
    # against both sides independently.
    add("Dateline West", 10.0, 179.98, "restaurant", "restaurant", "open", 0.7)
    add("Dateline West Cafe", 10.001, 179.985, "coffee_shop", "coffee_shop", "open", 0.65)
    add("Dateline East", 10.0, -179.98, "restaurant", "restaurant", "open", 0.7)
    add("Dateline East Bank", 9.999, -179.985, "bank", "bank", "open", 0.65)

    return rows


def box_wkt(lat_min: float, lat_max: float, lon_min: float, lon_max: float) -> str:
    """A rectangular polygon WKT string covering the given lat/lon box."""
    return (
        f"POLYGON(({lon_min} {lat_min}, {lon_min} {lat_max}, "
        f"{lon_max} {lat_max}, {lon_max} {lat_min}, {lon_min} {lat_min}))"
    )


def build_division_rows(con: duckdb.DuckDBPyConnection) -> list[tuple]:
    """Nested divisions around places.parquet's downtown, smallest-first by area.

    Every level here contains (CENTER_LAT, CENTER_LON); admin_lookup should
    return all five, neighborhood first. A sixth, unrelated polygon (around
    the high-latitude places cluster) proves non-containing divisions are
    excluded rather than just always-included.

    The `country` column (#188 follow-up) is what addresses.py's coverage
    check reads: it resolves the containing country by point-in-polygon over
    these rows rather than by nearest division point. The seventh polygon is
    a GB country covering the addresses fixture's deliberately-uncovered
    London coordinate, so that path has a polygon to be contained by.

    #225 adds two things every row now carries. `bbox` is the polygon's own
    extent, which is where geocode_address gets a city-sized box to scan
    addresses inside — the real theme carries it, and on `type=division_area`
    (unlike `type=division`) it is a genuine extent rather than a point's
    rounding envelope. `division_id` is the join back to the type=division
    entity: this fixture reuses each row's own id for it, the same shorthand
    that already lets DOWNTOWN_DIVISION_ID name a row in both fixtures.

    The last three polygons (#225) are the anchors geocode_address resolves:
    San Francisco and Mountain View around the Market St / Amphitheatre Pkwy
    address rows, with Mountain View's box copied from the live 2026-07-22.0
    extent, and Berlin for the German trailing-house-number case.
    """
    levels = [
        ("neighborhood", "US", box_wkt(40.695, 40.705, -73.905, -73.895)),
        ("locality", "US", box_wkt(40.6, 40.8, -74.0, -73.8)),
        ("county", "US", box_wkt(40.0, 41.0, -75.0, -73.0)),
        ("region", "US", box_wkt(39.0, 42.0, -76.0, -72.0)),
        ("country", "US", box_wkt(30.0, 50.0, -90.0, -60.0)),
        # Unrelated: near the high-latitude places cluster, doesn't contain CENTER.
        ("country", "NO", box_wkt(70.0, 85.0, 0.0, 30.0)),
        # #188: a country the addresses theme does not carry, around
        # build_geocode_fixture.py's UNCOVERED_LAT/UNCOVERED_LON.
        ("country", "GB", box_wkt(50.0, 55.0, -6.0, 2.0)),
    ]
    names = [
        "Downtown", "Metropolis", "Franklin County", "Empire State", "United Testland",
        "Arctica", "United Kingdom",
    ]
    rows = []
    for i, ((subtype, country, wkt), name) in enumerate(zip(levels, names)):
        (wkb,) = con.execute(f"SELECT ST_AsWKB(ST_GeomFromText('{wkt}'))").fetchone()
        division_id = gers_id(10_000 + i)
        bbox = _wkt_bbox(wkt)
        rows.append((division_id, {"primary": name}, subtype, country, wkb, bbox, division_id))
    for division_id, name, country, (xmin, ymin, xmax, ymax) in GEOCODE_ANCHOR_AREAS:
        wkt = box_wkt(ymin, ymax, xmin, xmax)
        (wkb,) = con.execute(f"SELECT ST_AsWKB(ST_GeomFromText('{wkt}'))").fetchone()
        rows.append((
            f"{division_id}-area", {"primary": name}, "locality", country, wkb,
            {"xmin": xmin, "ymin": ymin, "xmax": xmax, "ymax": ymax}, division_id,
        ))
    return rows


def _wkt_bbox(wkt: str) -> dict:
    """The bbox struct for one of box_wkt's rectangles, read back off its own
    corner list so the two can never disagree."""
    coords = wkt[len("POLYGON(("):-2].split(", ")
    pairs = [tuple(float(v) for v in c.split()) for c in coords]
    lons = [p[0] for p in pairs]
    lats = [p[1] for p in pairs]
    return {"xmin": min(lons), "ymin": min(lats), "xmax": max(lons), "ymax": max(lats)}


# #225: the city extents geocode_address anchors on, keyed by the
# build_geocode_fixture.py division id each one is the boundary of.
# (division_id, name, country, (xmin, ymin, xmax, ymax))
#
# Mountain View's box is the live 2026-07-22.0 extent verbatim
# (-122.1176,37.3542 .. -122.0449,37.4711), which is exactly the box R27's
# hand-guessed one was 0.002 degrees short of — so the fixture's anchor step
# is the same size as the real one, not a convenient rounding of it.
GEOCODE_ANCHOR_AREAS = (
    ("gers-div-san-francisco", "San Francisco", "US",
     (-122.5150, 37.7080, -122.3570, 37.8120)),
    ("gers-div-mountain-view", "Mountain View", "US",
     (-122.11756896972656, 37.35421371459961, -122.0449447631836, 37.4710693359375)),
    ("gers-div-berlin", "Berlin", "DE", (13.10, 52.35, 13.75, 52.65)),
    # A resolvable extent in a country the addresses theme does not carry at
    # all, so geocode_address has a case where the anchor step *succeeds* and
    # the emptiness is purely coverage — the note addresses.COVERED_COUNTRIES
    # backs. Without a boundary here the query would stop one step earlier
    # and never reach that note.
    ("gers-div-kensington-gb", "Kensington", "GB", (-0.22, 51.48, -0.16, 51.52)),
    # R28/#229: the wrong-country anchor repro, at fixture scale. Cambridge
    # resolves to the UK one (145,700 over Cambridge MA's 118,403), which has
    # *no* boundary here -- exactly the shape live "London" has, where the UK
    # London carries no division_area row at all. Only the Massachusetts one
    # does, so an anchor fallback that doesn't check the country walks across
    # the Atlantic and answers a UK query with US doorways.
    ("gers-div-cambridge-ma", "Cambridge", "US", (-71.16, 42.35, -71.06, 42.40)),
    # The same shape *within* one country, which is the case the fallback
    # legitimately exists for: Springfield resolves to the MA one (155,929),
    # which has no boundary, and the IL runner-up supplies one.
    ("gers-div-springfield-il", "Springfield", "US", (-89.75, 39.72, -89.58, 39.84)),
    # R28/#229: the quadrant repro (Washington's NW/SE streets) and the
    # house-number-parse one ("Calle 8, Miami"), each needing a real extent
    # for the scan step to be reached at all.
    ("gers-div-washington-dc", "Washington", "US", (-77.12, 38.79, -76.90, 39.00)),
    ("gers-div-miami", "Miami", "US", (-80.32, 25.70, -80.14, 25.86)),
)


# --- Buildings fixture (issue #23) -----------------------------------------
# An 8x10 grid (80 footprints — the buildings theme is huge in real life, so
# this stays deliberately small) of synthetic rectangular footprints around
# places.parquet's downtown cluster. Each rectangle is built directly in
# degree-space from a width_m/depth_m pair using the exact same
# meters<->degrees conversion buildings.py's _area_m2 applies when reading
# it back — so a rectangle's true area is exactly width_m * depth_m, and
# tests can compute ground-truth areas from the generator's own dimensions
# (no shapely, no float slop) rather than trusting the query layer's math to
# grade itself.
BUILDING_GRID_ROWS = 8
BUILDING_GRID_COLS = 10  # 8 * 10 = 80 footprints
BUILDING_SPACING_M = 25.0  # grid pitch, center to center
# Cycled by index so width/depth/subtype/height coverage are all
# deterministic and evenly distributed across the grid.
BUILDING_WIDTHS_M = [6.0, 9.0, 12.0, 15.0, 18.0]
BUILDING_DEPTHS_M = [8.0, 10.0, 12.0, 14.0, 16.0, 18.0, 20.0]
BUILDING_SUBTYPES = ["residential", "commercial", "industrial", None]
BUILDING_CLASS_BY_SUBTYPE = {
    "residential": "house", "commercial": "retail", "industrial": "warehouse", None: None,
}
# height/num_floors set for roughly a third of rows (idx % 3 == 0) — sparse,
# matching real Overture coverage; the other two thirds are None so
# summarize_buildings' *_known_pct has something real to report.
BUILDING_HEIGHT_STRIDE = 3
BUILDING_FLOOR_HEIGHT_M = 3.2


def build_building_rows(con: duckdb.DuckDBPyConnection) -> list[tuple]:
    """80 synthetic footprints on a grid centered on CENTER_LAT/CENTER_LON.

    Returns rows shaped (id, geometry WKB, bbox, subtype, class, height,
    num_floors) matching buildings.py's REQUIRED_COLUMNS. See the module
    comment above for why each footprint's degree-space rectangle is built
    from its exact width_m/depth_m rather than an offset_point() geodesic
    walk: it keeps ground-truth areas exact for tests.
    """
    mpd_lat = METERS_PER_DEGREE_LAT
    mpd_lon = METERS_PER_DEGREE_LAT * math.cos(math.radians(CENTER_LAT))
    dlat_spacing = BUILDING_SPACING_M / mpd_lat
    dlon_spacing = BUILDING_SPACING_M / mpd_lon

    rows = []
    idx = 0
    for i in range(BUILDING_GRID_ROWS):
        for j in range(BUILDING_GRID_COLS):
            base_lat = CENTER_LAT + (i - BUILDING_GRID_ROWS / 2) * dlat_spacing
            base_lon = CENTER_LON + (j - BUILDING_GRID_COLS / 2) * dlon_spacing
            width_m = BUILDING_WIDTHS_M[idx % len(BUILDING_WIDTHS_M)]
            depth_m = BUILDING_DEPTHS_M[idx % len(BUILDING_DEPTHS_M)]
            dlon = width_m / mpd_lon
            dlat = depth_m / mpd_lat
            lat_max, lon_max = base_lat + dlat, base_lon + dlon

            subtype = BUILDING_SUBTYPES[idx % len(BUILDING_SUBTYPES)]
            cls = BUILDING_CLASS_BY_SUBTYPE[subtype]
            height, num_floors = None, None
            if idx % BUILDING_HEIGHT_STRIDE == 0:
                num_floors = 1 + (idx % 5)
                height = round(num_floors * BUILDING_FLOOR_HEIGHT_M, 1)

            wkt = box_wkt(base_lat, lat_max, base_lon, lon_max)
            (wkb,) = con.execute(f"SELECT ST_AsWKB(ST_GeomFromText('{wkt}'))").fetchone()
            bbox = {"xmin": base_lon, "ymin": base_lat, "xmax": lon_max, "ymax": lat_max}
            rows.append((
                gers_id(20_000 + idx), wkb, bbox, subtype, cls, height, num_floors,
            ))
            idx += 1
    return rows


def build_buildings(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("INSTALL spatial; LOAD spatial;")
    rows = build_building_rows(con)
    con.execute("""
        CREATE TABLE buildings (
            id VARCHAR,
            geometry BLOB,
            bbox STRUCT(xmin DOUBLE, ymin DOUBLE, xmax DOUBLE, ymax DOUBLE),
            subtype VARCHAR,
            class VARCHAR,
            height DOUBLE,
            num_floors INTEGER
        )
    """)
    con.executemany("INSERT INTO buildings VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    con.execute(f"COPY buildings TO '{BUILDINGS_FIXTURE_PATH}' (FORMAT PARQUET)")
    print(f"wrote {len(rows)} rows to {BUILDINGS_FIXTURE_PATH}")


def build_places(con: duckdb.DuckDBPyConnection) -> None:
    rows = build_place_rows()
    con.execute("""
        CREATE TABLE places (
            id VARCHAR,
            bbox STRUCT(xmin DOUBLE, ymin DOUBLE, xmax DOUBLE, ymax DOUBLE),
            names STRUCT("primary" VARCHAR),
            taxonomy STRUCT("primary" VARCHAR, alternates VARCHAR[]),
            basic_category VARCHAR,
            operating_status VARCHAR,
            confidence DOUBLE,
            addresses STRUCT(
                freeform VARCHAR, locality VARCHAR, region VARCHAR,
                postcode VARCHAR, country VARCHAR
            )[],
            websites VARCHAR[],
            phones VARCHAR[],
            socials VARCHAR[],
            brand STRUCT(names STRUCT("primary" VARCHAR)),
            sources STRUCT(dataset VARCHAR, record_id VARCHAR)[]
        )
    """)
    con.executemany(
        "INSERT INTO places VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows
    )
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    con.execute(f"COPY places TO '{PLACES_FIXTURE_PATH}' (FORMAT PARQUET)")
    print(f"wrote {len(rows)} rows to {PLACES_FIXTURE_PATH}")


def build_division_areas(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("INSTALL spatial; LOAD spatial;")
    rows = build_division_rows(con)
    con.execute("""
        CREATE TABLE division_areas (
            id VARCHAR,
            names STRUCT("primary" VARCHAR),
            subtype VARCHAR,
            country VARCHAR,
            geometry BLOB,
            bbox STRUCT(xmin DOUBLE, ymin DOUBLE, xmax DOUBLE, ymax DOUBLE),
            division_id VARCHAR
        )
    """)
    con.executemany("INSERT INTO division_areas VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    con.execute(f"COPY division_areas TO '{DIVISION_AREAS_FIXTURE_PATH}' (FORMAT PARQUET)")
    print(f"wrote {len(rows)} rows to {DIVISION_AREAS_FIXTURE_PATH}")


def main() -> None:
    con = duckdb.connect()
    build_places(con)
    build_division_areas(con)
    build_buildings(con)


if __name__ == "__main__":
    main()
