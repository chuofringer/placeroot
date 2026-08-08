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
    # #214/R28: a division several of whose alternates match one query.
    # Quebec City's Overture primary is "Ville de Québec" and its common
    # names include "Quebec City", "Quebec" and localized variants -- three
    # separate folded alternates, all matching "Quebec", which returned the
    # same GERS id three times, one row per alternate.
    add(
        "gers-div-quebec-city", "Ville de Québec", "locality", "CA", "CA-QC",
        46.81, -71.21, _chain("Canada", "Québec", "Ville de Québec"),
        population=549_459,
        common={"en": "Quebec City", "es": "Quebec", "de": "Quebec Stadt"},
    )
    # #221: the two pairs the ranking fix is measured on, each mirroring the
    # live 2026-07-22.0 shape the R26 probes hit.
    #
    # "Zurich" literally matches this tiny Dutch village — and it carries a
    # population, so the #53 "literal match lacks prominence" gate saw a
    # good-enough literal answer and never ran the diacritic-folded pass that
    # is the only way to reach "Zürich" at all (ILIKE '%Zurich%' does not
    # match "Zürich"). The fold now always runs and ranking decides.
    add(
        "gers-div-zurich-ch", "Zürich", "locality", "CH", "CH-ZH",
        47.37, 8.54, _chain("Switzerland", "Zürich"),
        population=443_037,
    )
    add(
        "gers-div-zurich-nl", "Zurich", "locality", "NL", "NL-FR",
        53.13, 5.38, _chain("Netherlands", "Friesland", "Zurich"),
        population=190,
    )
    # "東京" is only a *prefix* of 東京都 (13.9M) but an *exact* match for this
    # population-less Nagano neighborhood, which is what tier-dominates-
    # prominence ranking put first. 東京都 already exists above.
    add(
        "gers-div-tokyo-nagano", "東京", "neighborhood", "JP", "JP-20",
        36.65, 138.18, _chain("Japan", "長野県", "東京"),
    )
    # #221 regression corpus: two same-named pairs where *both* sides carry a
    # population, so they pin that populated-vs-populated ordering is
    # untouched by the prominence rescue — Cambridge UK over Cambridge MA,
    # Portland OR over Portland ME (real figures, both directions checkable).
    add(
        "gers-div-cambridge-gb", "Cambridge", "locality", "GB", "GB-ENG",
        52.21, 0.12, _chain("United Kingdom", "England", "Cambridge"),
        population=145_700,
    )
    add(
        "gers-div-cambridge-ma", "Cambridge", "locality", "US", "US-MA",
        42.37, -71.11, _chain("United States", "Massachusetts", "Cambridge"),
        population=118_403,
    )
    add(
        "gers-div-portland-or", "Portland", "locality", "US", "US-OR",
        45.52, -122.68, _chain("United States", "Oregon", "Portland"),
        population=652_503,
    )
    add(
        "gers-div-portland-me", "Portland", "locality", "US", "US-ME",
        43.66, -70.26, _chain("United States", "Maine", "Portland"),
        population=68_408,
    )
    # #222/R28: the four cross-border wrong answers the prominence rescue
    # produced when any non-null population counted as prominence. Each pair
    # is the same shape: the place the caller means carries no population in
    # Overture and matches the query exactly; a different place in a
    # different country matches it only as a *prefix* and carries a
    # placeholder population (the live rows carry 1), which used to be
    # enough to leapfrog the exact match. _PROMINENCE_RESCUE_FLOOR is what
    # they now fail to clear.
    add(
        "gers-div-rafah-ps", "Rafah", "locality", "PS", "PS-GZ",
        31.29, 34.25, _chain("Palestine", "Gaza Strip", "Rafah"),
    )
    # The Saudi decoy reaches the query through an alternate spelling (the
    # transliterations of رفحاء vary); the alternate is what makes the
    # prefix relation to "Rafah" explicit, and the alt-name path is where
    # the live hit came from.
    add(
        "gers-div-rafha-sa", "Rafha", "locality", "SA", "SA-04",
        29.62, 43.49, _chain("Saudi Arabia", "Northern Borders", "Rafha"),
        population=1, common={"en": "Rafaha"},
    )
    add(
        "gers-div-johor-my", "Johor", "region", "MY", "MY-01",
        1.94, 103.36, _chain("Malaysia", "Johor"),
    )
    add(
        "gers-div-johor-bahru", "Johor Bahru", "locality", "MY", "MY-01",
        1.49, 103.74, _chain("Malaysia", "Johor", "Johor Bahru"), population=1,
    )
    add(
        "gers-div-enga-pg", "Enga", "region", "PG", "PG-EPW",
        -5.35, 143.55, _chain("Papua New Guinea", "Enga"),
    )
    add(
        "gers-div-engativa-co", "Engativá", "locality", "CO", "CO-DC",
        4.71, -74.11, _chain("Colombia", "Bogotá", "Engativá"), population=1,
    )
    add(
        "gers-div-plateau-lc", "Plateau", "neighborhood", "LC", "LC-11",
        13.83, -60.95, _chain("Saint Lucia", "Castries", "Plateau"),
    )
    add(
        "gers-div-plateau-central-bf", "Plateau-Central", "region", "BF", "BF-11",
        12.25, -0.75, _chain("Burkina Faso", "Plateau-Central"), population=1,
    )
    # #222/R28 positive control: the rescue itself must keep working. 河南 is
    # exactly the name of a population-less county in Qinghai and only a
    # prefix of 河南省 and its 99M people — the same shape as 東京/東京都
    # above, and the one a floor set too high would break.
    add(
        "gers-div-henan-cn", "河南省", "region", "CN", "CN-HA",
        33.88, 113.61, _chain("China", "河南省"), population=99_365_000,
    )
    add(
        "gers-div-henan-qinghai", "河南", "locality", "CN", "CN-QH",
        34.73, 101.61, _chain("China", "青海省", "河南"),
    )
    # #223: the divisions the postcode answer names. "Mission District" is the
    # locality-ish row nearest the synthetic 94110 US cluster below, so
    # geocode("94110") can report *where* the centroid is rather than bare
    # coordinates -- mirroring the live R27 probe, which put 94110's US
    # centroid in the Mission in San Francisco. Amsterdam does the same job
    # for the NL "1011AB" cluster.
    add(
        "gers-div-mission-district", "Mission District", "neighborhood", "US", "US-CA",
        37.7599, -122.4148,
        _chain("United States", "California", "San Francisco", "Mission District"),
    )
    add(
        "gers-div-amsterdam", "Amsterdam", "locality", "NL", "NL-NH",
        52.3728, 4.8936, _chain("Netherlands", "North Holland", "Amsterdam"),
        population=921_402,
    )
    # #225: the two anchors geocode_address resolves a street search inside.
    # San Francisco is already above (the #215 fuzzy target); these add the
    # Mountain View that "1600 Amphitheatre Parkway, Mountain View" anchors on
    # and the Berlin that carries the German trailing-house-number case. Both
    # have a matching division_area row in division_areas.parquet keyed on
    # their id -- without it there is no extent and geocode_address declines
    # to scan, which is its own test.
    add(
        "gers-div-mountain-view", "Mountain View", "locality", "US", "US-CA",
        37.3861, -122.0839, _chain("United States", "California", "Mountain View"),
        population=82_376,
    )
    add(
        "gers-div-berlin", "Berlin", "locality", "DE", "DE-BE",
        52.52, 13.405, _chain("Germany", "Berlin"),
        population=3_677_472,
    )
    # R28/#229: the quadrant case. Washington's streets carry NW/NE/SW/SE as
    # part of the name, and Overture writes the abbreviation -- so
    # "Pennsylvania Avenue NW" only matches through the quadrant variant map,
    # and a bare "Pennsylvania Avenue" only through a prefix match.
    add(
        "gers-div-washington-dc", "Washington", "locality", "US", "US-DC",
        38.9072, -77.0369, _chain("United States", "District of Columbia", "Washington"),
        population=689_545,
    )
    # R28/#229: the house-number parse case. "Calle 8" is the *street*; a
    # rule that strips a trailing integer searches for a street named
    # "Calle" and finds nothing.
    add(
        "gers-div-miami", "Miami", "locality", "US", "US-FL",
        25.7617, -80.1918, _chain("United States", "Florida", "Miami"),
        population=442_241,
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
    rows += build_postcode_addresses()
    rows += build_street_addresses()
    rows += build_shared_bbox_addresses()
    return rows


# #225: the street-level rows geocode_address searches, spelled the way
# Overture actually spells them on release 2026-07-22.0 -- UPPERCASE and
# USPS-abbreviated ("MARKET ST", "AMPHITHEATRE PKWY", both verified live). A
# query for "Market Street" therefore only finds them through the suffix
# variant map, which is the point: a fixture written as "Market Street" would
# pass without the feature under test.
#
# (street, country, postcode, postal_city, level, lat, lon, numbers, copies)
#
# `copies` is the dedup probe. Overture files one address point per source
# contribution, so the live MARKET ST in San Francisco is 2,980 rows over 900
# distinct number|street pairs; three copies of every Market St number here
# reproduce that shape at fixture scale, and an undeduplicated top-5 would
# return one doorway five times.
STREET_CLUSTERS = (
    ("MARKET ST", "US", "94103", "San Francisco", "CA", 37.7749, -122.4194,
     tuple(str(n) for n in range(1, 13)), 3),
    ("AMPHITHEATRE PKWY", "US", "94043", "Mountain View", "CA", 37.4220, -122.0841,
     ("1600", "1601", "1900"), 1),
    # No transformation needed for DE (R27-verified): "Hauptstraße" is one
    # token in the query and one in the data, so this cluster only exercises
    # the trailing-house-number parse ("Hauptstraße 5").
    ("Hauptstraße", "DE", "10827", "Berlin", None, 52.5200, 13.4050,
     ("5", "7", "9"), 1),
    # R28/#229: one street name, two quadrants -- different streets, and
    # neither is reachable from "Pennsylvania Avenue NW" without the
    # quadrant variant map or from "Pennsylvania Avenue" without a prefix
    # match. They stay separate rows in the answer, which is the point.
    ("PENNSYLVANIA AVE NW", "US", "20500", "Washington", "DC", 38.8977, -77.0365,
     ("1600", "1700"), 1),
    ("PENNSYLVANIA AVE SE", "US", "20003", "Washington", "DC", 38.8810, -76.9900,
     ("1600",), 1),
    # R28/#229: Calle Ocho. The street name ends in the digit.
    ("CALLE 8", "US", "33135", "Miami", "FL", 25.7650, -80.2200,
     ("1", "3", "5"), 1),
)

# Spacing between consecutive house numbers along a street, and between the
# duplicate copies of one number. The duplicate offset is deliberately tiny
# (sub-metre): duplicates are the same doorway contributed twice, not
# neighbours, so a dedup that kept them would show five rows metres apart.
STREET_NUMBER_STEP_DEG = 0.0006
STREET_DUPLICATE_OFFSET_DEG = 0.000004


def build_street_addresses() -> list[tuple]:
    """Address points along the named streets of STREET_CLUSTERS."""
    rows = []
    for street, country, postcode, city, level, lat, lon, numbers, copies in STREET_CLUSTERS:
        for i, number in enumerate(numbers):
            for copy in range(copies):
                rows.append((
                    f"gers-addr-st-{street.lower().replace(' ', '-')}-{number}-{copy}",
                    _point_bbox(
                        lat + i * STREET_NUMBER_STEP_DEG + copy * STREET_DUPLICATE_OFFSET_DEG,
                        lon + i * STREET_NUMBER_STEP_DEG,
                    ),
                    country,
                    number,
                    street,
                    None,
                    postcode,
                    city,
                    [{"value": level}] if level else None,
                ))
    return rows


# R28/#229: two municipalities inside one city's bounding box, and one
# doorway with many units — the two shapes that made the address dedup lie.
#
# Live, Boston's bbox covers Hingham, Charlestown and Cambridge, each with
# its own "1 MAIN ST"; grouping on (number, street) merged all three into one
# arbitrarily-picked row. And 1 FRANKLIN ST carries 458 units, of which the
# old arg_min(unit, d) named exactly one, nondeterministically.
#
# Both are reproduced inside the San Francisco anchor box (the fixture's one
# large extent): a Daly City postcode that the box spills over, and a
# multi-unit doorway. (number, street, postcode, postal_city, lat, lon, unit)
SHARED_BBOX_ADDRESSES = (
    ("1", "MAIN ST", "94112", "San Francisco", 37.7200, -122.4400, None),
    ("1", "MAIN ST", "94014", "Daly City", 37.7100, -122.4450, None),
    ("3", "MAIN ST", "94112", "San Francisco", 37.7205, -122.4405, None),
    ("3", "MAIN ST", "94014", "Daly City", 37.7105, -122.4455, None),
    # One doorway, three units: `unit` is unanswerable, `unit_count` is not.
    ("1", "FRANKLIN ST", "94102", "San Francisco", 37.7780, -122.4210, "Apt 1"),
    ("1", "FRANKLIN ST", "94102", "San Francisco", 37.7780, -122.4210, "Apt 2"),
    ("1", "FRANKLIN ST", "94102", "San Francisco", 37.7780, -122.4210, "Apt 3"),
    # ...and one with exactly one unit, which stays nameable.
    ("3", "FRANKLIN ST", "94102", "San Francisco", 37.7785, -122.4215, "Suite 900"),
)


def build_shared_bbox_addresses() -> list[tuple]:
    rows = []
    for i, (number, street, postcode, city, lat, lon, unit) in enumerate(SHARED_BBOX_ADDRESSES):
        rows.append((
            f"gers-addr-shared-{i:02d}",
            _point_bbox(lat, lon),
            "US",
            number,
            street,
            unit,
            postcode,
            city,
            [{"value": "CA"}],
        ))
    return rows


# #223: the postcode clusters geocode("94110") / geocode("1011AB") answer from.
# Synthetic, but shaped like what the live R27 probe measured on release
# 2026-07-22.0: one code carried by three different countries, with the US
# cluster the largest, SK second and FR third (live: 29,956 / 3,491 / 3,310) --
# so the fixture exercises a genuinely ambiguous postcode, not a tidy one.
# Each cluster sits on a division already in the divisions fixture, which is
# what the covering-locality join has to find: the Mission District (SF), then
# Bratislava and the small French "St. Louis", then Amsterdam.
#
# "1011AB" is stored unspaced, the way the Dutch source data writes it, so
# querying "1011 AB" only works if the spaced/unspaced variant pair really is
# searched. GB is deliberately absent from every cluster: the addresses theme
# does not cover it, which is what makes "SW1A 1AA" the empty-but-valid-shaped
# case the coverage note exists for.
POSTCODE_CLUSTERS = (
    # (postcode, country, postal_city, admin level, lat, lon, count)
    ("94110", "US", "San Francisco", "CA", 37.7599, -122.4148, 12),
    ("94110", "SK", "Bratislava", None, 48.1500, 17.1100, 5),
    ("94110", "FR", "St. Louis", None, 45.5000, 4.8000, 3),
    ("1011AB", "NL", "Amsterdam", None, 52.3728, 4.8936, 4),
)

# Cluster points are spread over a few hundred meters so the centroid is an
# average of scattered points (what a real postcode aggregate computes), not a
# single repeated coordinate.
POSTCODE_SPREAD_DEG = 0.002


def build_postcode_addresses() -> list[tuple]:
    """Address points carrying a queryable postcode, per POSTCODE_CLUSTERS.

    Offsets are symmetric around the cluster centre on purpose: the mean of
    each cluster lands back on the stated lat/lon, so a test can assert the
    centroid against the division it is supposed to name.
    """
    rows = []
    for postcode, country, city, level, lat, lon, count in POSTCODE_CLUSTERS:
        for i in range(count):
            # Symmetric around 0: for count points, offsets are
            # (i - (count-1)/2) steps out, which sums to zero.
            step = (i - (count - 1) / 2) * POSTCODE_SPREAD_DEG
            rows.append((
                f"gers-addr-pc-{country.lower()}-{postcode.lower()}-{i:02d}",
                _point_bbox(lat + step, lon - step),
                country,
                str(1 + i),
                STREET_NAMES[i % len(STREET_NAMES)],
                None,
                postcode,
                city,
                [{"value": level}] if level else None,
            ))
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
