"""Builds the offline test fixture: tests/fixtures/places.parquet.

Synthetic Overture-shaped places data (struct columns matching what
overture.py expects: bbox, names, taxonomy, basic_category,
operating_status, confidence). Deterministic — same seed, same output —
so it can be regenerated and diffed. Run with:

    uv run python scripts/build_fixture.py
"""

import math
import random
from pathlib import Path

import duckdb

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "places.parquet"
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


def build_rows() -> list[tuple]:
    rng = random.Random(SEED)
    rows = []

    def add(name, lat, lon, category, basic_category, status, confidence, alternates=None):
        rows.append((
            f"gers-{len(rows):05d}",
            {"xmin": lon, "ymin": lat, "xmax": lon, "ymax": lat},
            {"primary": name},
            {"primary": category, "alternates": alternates or []},
            basic_category,
            status,
            confidence,
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
        add(
            name, lat, lon,
            category if not uncategorized else "exotic_niche",
            None if uncategorized else category,
            status, confidence,
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


def main() -> None:
    rows = build_rows()
    con = duckdb.connect()
    con.execute("""
        CREATE TABLE places (
            id VARCHAR,
            bbox STRUCT(xmin DOUBLE, ymin DOUBLE, xmax DOUBLE, ymax DOUBLE),
            names STRUCT("primary" VARCHAR),
            taxonomy STRUCT("primary" VARCHAR, alternates VARCHAR[]),
            basic_category VARCHAR,
            operating_status VARCHAR,
            confidence DOUBLE
        )
    """)
    con.executemany("INSERT INTO places VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    con.execute(f"COPY places TO '{FIXTURE_PATH}' (FORMAT PARQUET)")
    print(f"wrote {len(rows)} rows to {FIXTURE_PATH}")


if __name__ == "__main__":
    main()
