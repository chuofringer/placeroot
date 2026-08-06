"""Builds the offline geocoding fixtures (#10): divisions.parquet and addresses.parquet.

Synthetic Overture-shaped divisions (locality/neighborhood/region/country,
with a "hierarchies" containing-chain column) and a grid of synthetic street
addresses, both in the same bbox-struct-as-point-geometry convention
build_fixture.py uses for places. Deterministic — same seed, same output —
so it can be regenerated and diffed. Run with:

    uv run python scripts/build_geocode_fixture.py
"""

import math
from pathlib import Path

import duckdb

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
DIVISIONS_PATH = FIXTURES_DIR / "divisions.parquet"
ADDRESSES_PATH = FIXTURES_DIR / "addresses.parquet"

# Same fake "downtown" center as build_fixture.py's places fixture, so
# reverse_geocode tests can find both places and addresses near one point.
CENTER_LAT = 40.700000
CENTER_LON = -73.900000

ADDRESS_GRID_ROWS = 15
ADDRESS_GRID_COLS = 20
ADDRESS_SPACING_M = 15.0
STREET_NAMES = ["Main St", "Oak Ave", "1st St", "River Rd", "Elm St"]


def _point_bbox(lat: float, lon: float) -> dict:
    return {"xmin": lon, "ymin": lat, "xmax": lon, "ymax": lat}


def _chain(*names: str) -> list[list[dict]]:
    """One hierarchy path: {"name": ...} dicts, top-level ancestor first, the division

    itself last — matches live Overture divisions data (verified against a real
    "Seattle" lookup), so geocode.py's admin_context stripping logic is exercised
    the same way against this fixture as it will be against the real dataset.
    """
    return [[{"name": n} for n in names]]


def build_divisions() -> list[tuple]:
    rows = []

    def add(division_id, name, subtype, country, region, lat, lon, hierarchies):
        rows.append((
            division_id,
            _point_bbox(lat, lon),
            {"primary": name},
            subtype,
            country,
            region,
            hierarchies,
        ))

    add("gers-div-us", "United States", "country", "US", None, 39.8, -98.5, _chain("United States"))
    add(
        "gers-div-ny", "New York", "region", "US", "US-NY", 43.0, -75.0,
        _chain("United States", "New York"),
    )
    add(
        "gers-div-il", "Illinois", "region", "US", "US-IL", 40.0, -89.0,
        _chain("United States", "Illinois"),
    )
    add(
        "gers-div-ma", "Massachusetts", "region", "US", "US-MA",
        42.4, -71.4, _chain("United States", "Massachusetts"),
    )
    add(
        "gers-div-springfield-il", "Springfield", "locality", "US", "US-IL",
        39.78, -89.65, _chain("United States", "Illinois", "Springfield"),
    )
    add(
        "gers-div-springfield-ma", "Springfield", "locality", "US", "US-MA",
        42.10, -72.59, _chain("United States", "Massachusetts", "Springfield"),
    )
    add(
        "gers-div-brooklyn", "Brooklyn", "locality", "US", "US-NY",
        CENTER_LAT, CENTER_LON, _chain("United States", "New York", "Brooklyn"),
    )
    add(
        "gers-div-downtown-brooklyn", "Downtown Brooklyn", "neighborhood", "US", "US-NY",
        CENTER_LAT + 0.001, CENTER_LON - 0.001,
        _chain("United States", "New York", "Brooklyn", "Downtown Brooklyn"),
    )
    add(
        "gers-div-riverside", "Riverside", "neighborhood", "US", "US-IL",
        39.79, -89.64, _chain("United States", "Illinois", "Springfield", "Riverside"),
    )
    return rows


def build_addresses() -> list[tuple]:
    rows = []
    dlat = ADDRESS_SPACING_M / 111_320.0
    dlon = ADDRESS_SPACING_M / (111_320.0 * math.cos(math.radians(CENTER_LAT)))
    n = 0
    for row in range(ADDRESS_GRID_ROWS):
        for col in range(ADDRESS_GRID_COLS):
            lat = CENTER_LAT + (row - ADDRESS_GRID_ROWS // 2) * dlat
            lon = CENTER_LON + (col - ADDRESS_GRID_COLS // 2) * dlon
            street = STREET_NAMES[n % len(STREET_NAMES)]
            number = str(100 + n)
            rows.append((
                f"gers-addr-{n:05d}",
                _point_bbox(lat, lon),
                street,
                number,
                "11201",
            ))
            n += 1
    return rows


def main() -> None:
    con = duckdb.connect()

    divisions = build_divisions()
    con.execute("""
        CREATE TABLE divisions (
            id VARCHAR,
            bbox STRUCT(xmin DOUBLE, ymin DOUBLE, xmax DOUBLE, ymax DOUBLE),
            names STRUCT("primary" VARCHAR),
            subtype VARCHAR,
            country VARCHAR,
            region VARCHAR,
            hierarchies STRUCT("name" VARCHAR)[][]
        )
    """)
    con.executemany("INSERT INTO divisions VALUES (?, ?, ?, ?, ?, ?, ?)", divisions)
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    con.execute(f"COPY divisions TO '{DIVISIONS_PATH}' (FORMAT PARQUET)")
    print(f"wrote {len(divisions)} rows to {DIVISIONS_PATH}")

    addresses = build_addresses()
    con.execute("""
        CREATE TABLE addresses (
            id VARCHAR,
            bbox STRUCT(xmin DOUBLE, ymin DOUBLE, xmax DOUBLE, ymax DOUBLE),
            street VARCHAR,
            number VARCHAR,
            postcode VARCHAR
        )
    """)
    con.executemany("INSERT INTO addresses VALUES (?, ?, ?, ?, ?)", addresses)
    con.execute(f"COPY addresses TO '{ADDRESSES_PATH}' (FORMAT PARQUET)")
    print(f"wrote {len(addresses)} rows to {ADDRESSES_PATH}")


if __name__ == "__main__":
    main()
