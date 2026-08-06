"""Builds the offline test fixtures: tests/fixtures/places.parquet and
tests/fixtures/divisions.parquet.

places.parquet: synthetic Overture-shaped places data (struct columns
matching what overture.py expects: id, bbox, names, taxonomy,
basic_category, operating_status, confidence, addresses, websites, phones,
socials, brand, sources — the last six back place_details, issue #9).
GERS ids (issue #25) are deterministic 32-char hex strings derived from each
row's index, standing in for Overture's real GERS ids without claiming to
be one.

divisions.parquet: a small synthetic divisions-theme fixture (issue #11) —
a handful of nested rectangular polygons (neighborhood < locality < county
< region < country) around places.parquet's downtown cluster, plus one
unrelated polygon far away to prove point-in-polygon actually excludes
non-containing divisions. Real division_area geometry is irregular; these
are rectangles because admin_lookup only needs correct containment
semantics, not realistic shapes.

Both are deterministic — same seed, same output — so they can be
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
DIVISIONS_FIXTURE_PATH = FIXTURES_DIR / "divisions.parquet"
SEED = 42
EARTH_RADIUS_M = 6371000.0

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

    # Antimeridian pair — documents (does not fix) lack of seam handling.
    add("Dateline West", 10.0, 179.98, "restaurant", "restaurant", "open", 0.7)
    add("Dateline East", 10.0, -179.98, "restaurant", "restaurant", "open", 0.7)

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
    """
    levels = [
        ("neighborhood", box_wkt(40.695, 40.705, -73.905, -73.895)),
        ("locality", box_wkt(40.6, 40.8, -74.0, -73.8)),
        ("county", box_wkt(40.0, 41.0, -75.0, -73.0)),
        ("region", box_wkt(39.0, 42.0, -76.0, -72.0)),
        ("country", box_wkt(30.0, 50.0, -90.0, -60.0)),
        # Unrelated: near the high-latitude places cluster, doesn't contain CENTER.
        ("country", box_wkt(70.0, 85.0, 0.0, 30.0)),
    ]
    names = [
        "Downtown", "Metropolis", "Franklin County", "Empire State", "United Testland",
        "Arctica",
    ]
    rows = []
    for i, ((subtype, wkt), name) in enumerate(zip(levels, names)):
        (wkb,) = con.execute(f"SELECT ST_AsWKB(ST_GeomFromText('{wkt}'))").fetchone()
        rows.append((gers_id(10_000 + i), {"primary": name}, subtype, wkb))
    return rows


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


def build_divisions(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("INSTALL spatial; LOAD spatial;")
    rows = build_division_rows(con)
    con.execute("""
        CREATE TABLE divisions (
            id VARCHAR,
            names STRUCT("primary" VARCHAR),
            subtype VARCHAR,
            geometry BLOB
        )
    """)
    con.executemany("INSERT INTO divisions VALUES (?, ?, ?, ?)", rows)
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    con.execute(f"COPY divisions TO '{DIVISIONS_FIXTURE_PATH}' (FORMAT PARQUET)")
    print(f"wrote {len(rows)} rows to {DIVISIONS_FIXTURE_PATH}")


def main() -> None:
    con = duckdb.connect()
    build_places(con)
    build_divisions(con)


if __name__ == "__main__":
    main()
