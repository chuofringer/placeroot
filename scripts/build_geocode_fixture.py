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

# The GERS id of the "Downtown" polygon in division_areas.parquet. The
# division *entity* row below reuses it so the two fixtures join the way real
# Overture data does (division.id == division_area.division_id) — without a
# shared id, no division entity ever appears in its own admin_lookup chain,
# and gers_lookup's containing-division join has nothing realistic to test.
DOWNTOWN_DIVISION_ID = "2835be088c8011a4aee3dff5cabbcf13"
# The locality that *contains* Downtown, likewise a division_area id. Its
# entity row sits at the same point, so its own containment chain is
# [Downtown, Metropolis(self), Franklin County, ...] — a division that is not
# the smallest thing covering its own reference point, which is the shape
# that distinguishes "the division containing me" from "the next entry down".
METROPOLIS_DIVISION_ID = "9e34d836dceb18e1254bed9c0a40d455"

ADDRESS_GRID_ROWS = 15
ADDRESS_GRID_COLS = 20
ADDRESS_SPACING_M = 15.0
STREET_NAMES = ["Main St", "Oak Ave", "1st St", "River Rd", "Elm St"]

# #188: a coordinate in a country outside the addresses theme's coverage (see
# addresses.COVERED_COUNTRIES). Real Kensington, London — there is a division
# here in the fixture but deliberately no address point anywhere near it, so
# address_at's coverage note is what has to explain the empty answer.
UNCOVERED_LAT = 51.5000
UNCOVERED_LON = -0.1900

# Grid index of the address point sitting exactly on CENTER_LAT/CENTER_LON —
# the nearest row to every fixture query aimed at the centre, so it is where
# the awkward-but-real attribute shapes live (see build_addresses).
CENTER_ADDRESS_INDEX = (ADDRESS_GRID_ROWS // 2) * ADDRESS_GRID_COLS + ADDRESS_GRID_COLS // 2


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

    def add(
        division_id, name, subtype, country, region, lat, lon, hierarchies,
        population=None, common=None,
    ):
        # #214: `common` mirrors Overture's names.common — a language-code ->
        # localized-name map. Written for every row (empty when there are no
        # alternates) so the fixture's `names` struct has the same shape the
        # real theme does and geocode's alt-name materialization runs against
        # it unchanged.
        rows.append((
            division_id,
            _point_bbox(lat, lon),
            {"primary": name, "common": common or {}},
            subtype,
            country,
            region,
            hierarchies,
            population,
        ))

    add("gers-div-us", "United States", "country", "US", None, 39.8, -98.5, _chain("United States"))
    add(
        "gers-div-ny", "New York", "region", "US", "US-NY", 43.0, -75.0,
        _chain("United States", "New York"), population=19_571_216,
    )
    # Illinois deliberately outweighs Massachusetts in region population so
    # a same-named-pair test (Fairview, below) can exercise the "presence in
    # a more populous region" prominence proxy (#47) when neither candidate
    # carries its own population.
    add(
        "gers-div-il", "Illinois", "region", "US", "US-IL", 40.0, -89.0,
        _chain("United States", "Illinois"), population=12_582_032,
    )
    add(
        "gers-div-ma", "Massachusetts", "region", "US", "US-MA",
        42.4, -71.4, _chain("United States", "Massachusetts"), population=6_981_974,
    )
    add(
        "gers-div-oh", "Ohio", "region", "US", "US-OH", 40.4, -82.9,
        _chain("United States", "Ohio"), population=11_785_935,
    )
    # A non-US region, so parsing "London, Ontario" has to hit the general
    # region-subtype-name lookup rather than the embedded US state map (#46).
    add(
        "gers-div-ontario", "Ontario", "region", "CA", "CA-ON", 51.25, -85.32,
        _chain("Canada", "Ontario"),
    )
    # Real-world Springfield populations (MA > IL) so the population tiebreak
    # (#47) has a clear, honest answer to check against.
    add(
        "gers-div-springfield-il", "Springfield", "locality", "US", "US-IL",
        39.78, -89.65, _chain("United States", "Illinois", "Springfield"), population=114_230,
    )
    add(
        "gers-div-springfield-ma", "Springfield", "locality", "US", "US-MA",
        42.10, -72.59, _chain("United States", "Massachusetts", "Springfield"),
        population=155_929,
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
    # The entity twin of division_areas.parquet's "Downtown" polygon, sharing
    # its GERS id (see DOWNTOWN_DIVISION_ID). Sits at the fixture centre, so
    # it is contained by its own polygon and gers_lookup resolving this id
    # gets a chain whose first entry is itself.
    add(
        DOWNTOWN_DIVISION_ID, "Downtown", "neighborhood", "US", "US-NY",
        CENTER_LAT, CENTER_LON,
        _chain("United States", "New York", "Brooklyn", "Downtown"),
    )
    add(
        METROPOLIS_DIVISION_ID, "Metropolis", "locality", "US", "US-NY",
        CENTER_LAT, CENTER_LON,
        _chain("United States", "New York", "Metropolis"),
    )
    add(
        "gers-div-riverside", "Riverside", "neighborhood", "US", "US-IL",
        39.79, -89.64, _chain("United States", "Illinois", "Springfield", "Riverside"),
    )
    # Same name, two real Ohio/Ontario cities named "London" — exercises
    # "City, ST" (US abbreviation) and "City, Region" (general region-name)
    # parsing (#46) resolving to two different, correct candidates.
    add(
        "gers-div-london-oh", "London", "locality", "US", "US-OH",
        39.89, -83.45, _chain("United States", "Ohio", "London"),
    )
    add(
        "gers-div-london-on", "London", "locality", "CA", "CA-ON",
        42.98, -81.25, _chain("Canada", "Ontario", "London"),
    )
    # Same name, same subtype, neither carries a population value: exercises
    # the hierarchy-depth/region-population proxy chain (#47) rather than
    # the population tiebreak. Hierarchy depth ties (both 3 deep), so the
    # decisive step is Illinois's larger region population (above). IDs are
    # deliberately the *opposite* of the wanted order (fairview-ma sorts
    # before zz-fairview-il alphabetically) so this test only passes if the
    # region-population proxy actually ran — id is the last tiebreak, not
    # a coincidental first one.
    add(
        "gers-div-zz-fairview-il", "Fairview", "locality", "US", "US-IL",
        39.5, -89.0, _chain("United States", "Illinois", "Fairview"),
    )
    add(
        "gers-div-fairview-ma", "Fairview", "locality", "US", "US-MA",
        42.3, -71.8, _chain("United States", "Massachusetts", "Fairview"),
    )
    # Same name, different subtype, neither carries population: exercises
    # the subtype-rank step of the proxy chain (#47) ahead of hierarchy
    # depth/region population. IDs again deliberately inverted vs. the
    # wanted order for the same reason as Fairview above.
    add(
        "gers-div-zz-hilltop-loc", "Hilltop", "locality", "US", "US-IL",
        39.6, -89.2, _chain("United States", "Illinois", "Hilltop"),
    )
    add(
        "gers-div-hilltop-nbhd", "Hilltop", "neighborhood", "US", "US-NY",
        CENTER_LAT + 0.002, CENTER_LON + 0.002,
        _chain("United States", "New York", "Brooklyn", "Hilltop"),
    )
    # #53: Overture's canonical name uses the expanded "Saint" spelling —
    # a literal query for "St. Louis" (or "St Louis") must not find it
    # without the abbreviation-variant retry.
    add(
        "gers-div-saint-louis", "Saint Louis", "locality", "US", "US-MO",
        38.63, -90.20, _chain("United States", "Missouri", "Saint Louis"),
        population=301_578,
    )
    # #53: a tiny, unpopulated village that happens to be literally spelled
    # "St. Louis" (mirrors real Overture data, verified live) — the literal
    # query must not stop at this exact-tier-but-unpopulated match; the
    # populated "Saint Louis" variant above has to win on prominence.
    add(
        "gers-div-st-louis-tiny", "St. Louis", "locality", "FR", "FR-ARA",
        45.5, 4.8, _chain("France", "Auvergne-Rhone-Alpes", "St. Louis"),
    )
    # #53: canonical name carries a diacritic — a plain-ASCII query for
    # "Sao Paulo" must not find it without the diacritic-folded retry.
    add(
        "gers-div-sao-paulo", "São Paulo", "locality", "BR", "BR-SP",
        -23.55, -46.63, _chain("Brazil", "São Paulo"),
        population=12_300_000,
    )
    # #215: the three live-verified typo probes ("Berekley", "Cinncinati",
    # "Sna Francisco") need their correctly-spelled targets in the fixture
    # for the fuzzy fallback tier to have anything to correct *to*. All
    # three carry a population, like the well-known places they are, so the
    # fuzzy pass's population ordering is exercised on real-shaped rows.
    add(
        "gers-div-berkeley", "Berkeley", "locality", "US", "US-CA",
        37.87, -122.27, _chain("United States", "California", "Berkeley"),
        population=124_321,
    )
    add(
        "gers-div-cincinnati", "Cincinnati", "locality", "US", "US-OH",
        39.10, -84.51, _chain("United States", "Ohio", "Cincinnati"),
        population=309_317,
    )
    add(
        "gers-div-san-francisco", "San Francisco", "locality", "US", "US-CA",
        37.77, -122.42, _chain("United States", "California", "San Francisco"),
        population=808_437,
    )
    # #214: exonyms. Each of these four cities is stored under its endonym in
    # names.primary and reachable in English only through names.common — the
    # exact shape that made live "Munich" answer Munich, North Dakota. Each
    # comes with the small same-named decoy the live probe actually returned,
    # deliberately without a population, so the tests pin the *ranking* (#47
    # prominence beats a population-less literal namesake) and not merely
    # "the alternate matched something".
    add(
        "gers-div-munchen", "München", "locality", "DE", "DE-BY",
        48.14, 11.58, _chain("Germany", "Bavaria", "München"),
        population=1_512_491,
        common={"en": "Munich", "fr": "Munich", "it": "Monaco di Baviera", "es": "Múnich"},
    )
    add(
        "gers-div-munich-nd", "Munich", "locality", "US", "US-ND",
        48.67, -98.83, _chain("United States", "North Dakota", "Munich"),
    )
    add(
        "gers-div-tokyo-jp", "東京都", "locality", "JP", "JP-13",
        35.68, 139.69, _chain("Japan", "東京都"),
        population=13_929_286,
        common={"en": "Tokyo", "de": "Tokio", "fr": "Tokyo"},
    )
    add(
        "gers-div-tokyo-pg", "Tokyo", "locality", "PG", "PG-EBR",
        -4.35, 152.26, _chain("Papua New Guinea", "East New Britain", "Tokyo"),
    )
    add(
        "gers-div-moskva-ru", "Москва", "locality", "RU", "RU-MOW",
        55.75, 37.62, _chain("Russia", "Москва"),
        population=13_010_112,
        common={"en": "Moscow", "de": "Moskau", "cs": "Moskva", "pl": "Moskwa"},
    )
    add(
        "gers-div-moskva-tj", "Moskva", "locality", "TJ", "TJ-KT",
        37.60, 68.79, _chain("Tajikistan", "Khatlon", "Moskva"),
    )
    add(
        "gers-div-wien", "Wien", "locality", "AT", "AT-9",
        48.21, 16.37, _chain("Austria", "Wien"),
        population=1_982_097,
        common={"en": "Vienna", "it": "Vienna", "fr": "Vienne", "cs": "Vídeň"},
    )
    add(
        "gers-div-vienna-il", "Vienna", "locality", "US", "US-IL",
        37.41, -88.89, _chain("United States", "Illinois", "Vienna"),
    )
    # #214: an alternate whose only obstacle is a letter strip_accents leaves
    # alone (duckdb#15706) — "Preßburg" is the German exonym for Bratislava,
    # and a plain-ASCII "Pressburg" query only reaches it through the
    # explicit _UNFOLDED_LETTERS map on both sides of the comparison.
    add(
        "gers-div-bratislava", "Bratislava", "locality", "SK", "SK-BL",
        48.15, 17.11, _chain("Slovakia", "Bratislava"),
        population=475_503,
        common={"de": "Preßburg", "hu": "Pozsony"},
    )
    # #188: a division in a country the addresses theme does NOT cover (GB is
    # not one of addresses.COVERED_COUNTRIES), so address_at has somewhere to
    # resolve "this coordinate is in an uncovered country" from. Deliberately
    # not named "London" — the two fixture Londons above are load-bearing for
    # the ambiguity tests, and a third would change what they assert.
    add(
        "gers-div-kensington-gb", "Kensington", "locality", "GB", "GB-ENG",
        UNCOVERED_LAT, UNCOVERED_LON,
        _chain("United Kingdom", "England", "Kensington"),
    )
    return rows


def build_addresses() -> list[tuple]:
    """A grid of synthetic address points around the fixture centre.

    Columns follow Overture's addresses schema (#188): country/number/street
    /unit/postcode/postal_city/address_levels, with address_levels as the
    list-of-{value} struct the real theme uses. Two rows near the centre are
    deliberately awkward rather than uniform:

    - the exact-centre row carries a non-numeric house number ("74B") and a
      unit, because Overture's `number` is a string and real data is full of
      "74B"/"12 bis" — anything that parses it as an integer must break here;
    - its immediate neighbour carries no postcode, postal_city or
      address_levels at all, which is the common shape outside the
      best-covered countries and the case the optional-field omission is for.
    """
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
            unit = None
            postcode = "11201"
            postal_city = "Brooklyn"
            address_levels = [{"value": "NY"}]
            if n == CENTER_ADDRESS_INDEX:
                number, unit = "74B", "Apt 3"
            elif n == CENTER_ADDRESS_INDEX + 1:
                postcode, postal_city, address_levels = None, None, None
            rows.append((
                f"gers-addr-{n:05d}",
                _point_bbox(lat, lon),
                "US",
                number,
                street,
                unit,
                postcode,
                postal_city,
                address_levels,
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
            names STRUCT("primary" VARCHAR, common MAP(VARCHAR, VARCHAR)),
            subtype VARCHAR,
            country VARCHAR,
            region VARCHAR,
            hierarchies STRUCT("name" VARCHAR)[][],
            population BIGINT
        )
    """)
    con.executemany("INSERT INTO divisions VALUES (?, ?, ?, ?, ?, ?, ?, ?)", divisions)
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    con.execute(f"COPY divisions TO '{DIVISIONS_PATH}' (FORMAT PARQUET)")
    print(f"wrote {len(divisions)} rows to {DIVISIONS_PATH}")

    addresses = build_addresses()
    con.execute("""
        CREATE TABLE addresses (
            id VARCHAR,
            bbox STRUCT(xmin DOUBLE, ymin DOUBLE, xmax DOUBLE, ymax DOUBLE),
            country VARCHAR,
            number VARCHAR,
            street VARCHAR,
            unit VARCHAR,
            postcode VARCHAR,
            postal_city VARCHAR,
            address_levels STRUCT("value" VARCHAR)[]
        )
    """)
    con.executemany("INSERT INTO addresses VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", addresses)
    con.execute(f"COPY addresses TO '{ADDRESSES_PATH}' (FORMAT PARQUET)")
    print(f"wrote {len(addresses)} rows to {ADDRESSES_PATH}")


if __name__ == "__main__":
    main()
