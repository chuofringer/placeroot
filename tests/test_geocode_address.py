"""geocode_address: street-level forward search (issue #225).

Runs against the committed fixtures. The address rows the street search
matches are written the way Overture actually writes them on release
2026-07-22.0 — UPPERCASE and USPS-abbreviated, "MARKET ST" / "AMPHITHEATRE
PKWY", both verified live — so a query spelled "Market Street" can only find
them through the suffix variant map. A fixture spelled "Market Street" would
pass with the feature deleted, which is the whole reason it isn't.

The anchor extents come from division_areas.parquet's `bbox`/`division_id`
columns (scripts/build_fixture.py's GEOCODE_ANCHOR_AREAS): San Francisco,
Mountain View (the live extent verbatim), Berlin, and a GB Kensington that
exists purely so the anchor step can *succeed* in a country the addresses
theme does not carry — the coverage-note case.
"""

import pytest

from placeroot import addresses, geocode, server


def _streets(payload):
    return [(r["number"], r["street"]) for r in payload["results"]]


# --- the two acceptance queries -------------------------------------------


def test_street_and_city_returns_deduped_rows_on_that_street():
    result = geocode.geocode_address("Market Street, San Francisco")

    assert result["anchor"]["name"] == "San Francisco"
    assert result["results"], "the fixture carries MARKET ST rows in San Francisco"
    assert all(r["street"] == "MARKET ST" for r in result["results"])
    # Deduplicated: the fixture writes every Market St number three times.
    assert len(_streets(result)) == len(set(_streets(result)))


def test_market_street_reports_the_distinct_count_not_the_raw_row_count():
    result = geocode.geocode_address("Market Street, San Francisco", limit=5)

    # 12 distinct numbers x 3 duplicate contributions each.
    assert result["distinct_in_range"] == 12
    assert result["truncated"] is True
    assert "36 raw address points" in result["note"]
    assert len(result["results"]) == 5


def test_number_and_street_lands_on_the_exact_point():
    result = geocode.geocode_address("1600 Amphitheatre Parkway, Mountain View")

    assert result["anchor"]["name"] == "Mountain View"
    assert _streets(result) == [("1600", "AMPHITHEATRE PKWY")]
    row = result["results"][0]
    assert (row["lat"], row["lon"]) == (37.422, -122.0841)
    assert row["postcode"] == "94043"
    assert row["country"] == "US"
    # One doorway is not a truncated answer.
    assert "truncated" not in result


def test_trailing_house_number_is_the_german_convention():
    """"Hauptstraße 5" — the number is the *last* token, and the street name
    needs no suffix transformation at all (R27-verified for DE/NL)."""
    result = geocode.geocode_address("Hauptstraße 5, Berlin")

    assert _streets(result) == [("5", "Hauptstraße")]
    assert result["results"][0]["country"] == "DE"


# --- parsing ---------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("1600 Amphitheatre Pkwy", ("1600", "Amphitheatre Pkwy")),
        ("Hauptstraße 5", ("5", "Hauptstraße")),
        ("Market Street", (None, "Market Street")),
        # A bare number is a postcode-shaped thing for geocode(), not a house
        # number with an empty street.
        ("94110", (None, "94110")),
        # Mid-query digits are part of the street name, not the house number.
        ("West 42nd Street", (None, "West 42nd Street")),
    ],
)
def test_house_number_is_leading_or_trailing_only(text, expected):
    assert geocode._split_house_number(text) == expected


def test_first_comma_splits_street_from_place_and_the_rest_goes_to_geocode():
    assert geocode._parse_address_query("1600 Amphitheatre Parkway, Mountain View, CA") == (
        "1600", "Amphitheatre Parkway", "Mountain View, CA",
    )


def test_structured_params_override_the_parse():
    result = geocode.geocode_address(
        number="1600", street="Amphitheatre Pkwy", city="Mountain View"
    )
    assert _streets(result) == [("1600", "AMPHITHEATRE PKWY")]


def test_structured_city_rescues_a_query_with_no_comma():
    result = geocode.geocode_address("Market Street", city="San Francisco")
    assert result["results"]


# --- the USPS suffix map, both directions ----------------------------------


@pytest.mark.parametrize(
    "typed,reached",
    [
        ("Market Street", "Market St"),
        ("Market St", "Market Street"),
        ("Amphitheatre Parkway", "Amphitheatre Pkwy"),
        ("Amphitheatre Pkwy", "Amphitheatre Parkway"),
        ("Sunset Boulevard", "Sunset Blvd"),
        ("Sunset Blvd", "Sunset Boulevard"),
        ("Ocean Avenue", "Ocean Ave"),
        ("Ocean Ave", "Ocean Avenue"),
        ("River Road", "River Rd"),
        ("Elm Drive", "Elm Dr"),
        ("Oak Lane", "Oak Ln"),
        ("Union Court", "Union Ct"),
        ("Union Place", "Union Pl"),
    ],
)
def test_street_suffix_variants_are_bidirectional(typed, reached):
    variants = [v.lower() for v in geocode._street_variants(typed)]
    assert typed.lower() in variants, "the spelling the caller typed is always tried"
    assert reached.lower() in variants


def test_directionals_expand_anywhere_in_a_street_name():
    """A bare "N" mid-query is too ambiguous to expand in a *division* name,
    which is why _token_variants gates it on `leading` — a street field is
    bounded enough to lift that, and "W 42nd St" needs it."""
    variants = [v.lower() for v in geocode._street_variants("West 42nd Street")]
    assert "w 42nd st" in variants
    assert "west 42nd street" in variants
    # ...and the division-name path is untouched by the street map.
    assert geocode._token_variants("street", leading=False) == []
    assert geocode._token_variants("north", leading=False) == []


def test_street_variants_stay_capped():
    many = "North East Street Avenue Road Drive Lane Court Place"
    assert len(geocode._street_variants(many)) <= geocode._STREET_VARIANT_CAP


# --- no scan without a resolvable anchor -----------------------------------


def test_no_city_means_no_scan():
    result = geocode.geocode_address("Market Street")
    assert result["results"] == []
    assert "no city to search in" in result["note"]


def test_no_street_means_no_scan():
    result = geocode.geocode_address(", San Francisco")
    assert result["results"] == []
    assert "no street to search for" in result["note"]


def test_unresolvable_city_is_an_honest_empty():
    result = geocode.geocode_address("Market Street, Zzzzqqxx")
    assert result["results"] == []
    assert "did not resolve to any place" in result["note"]
    assert "anchor" not in result


def test_a_city_with_no_boundary_extent_declines_to_guess_one():
    """Brooklyn is in the divisions fixture but has no division_area row, so
    there is no extent — and guessing a radius would return doorways from
    whatever happens to be nearby."""
    result = geocode.geocode_address("Main St, Brooklyn")
    assert result["results"] == []
    assert "no boundary extent" in result["note"]
    assert "address_at(" in result["note"]


# --- dedup does not merge municipalities (R28 HIGH-4, #229) ----------------


def test_same_number_in_two_municipalities_stays_two_rows():
    """A city bbox is not a municipality: San Francisco's box spills over
    into Daly City, and both have a 1 MAIN ST. Live, Boston's box covers
    Hingham, Charlestown and Cambridge the same way, and grouping on
    (number, street) alone merged all three into one arbitrary row."""
    result = geocode.geocode_address("Main St, San Francisco", limit=10)

    rows = [(r["number"], r["street"], r["postcode"]) for r in result["results"]]
    assert ("1", "MAIN ST", "94112") in rows
    assert ("1", "MAIN ST", "94014") in rows
    assert len(rows) == len(set(rows))
    # Both municipalities' 1 and 3 survive as their own rows.
    assert len([r for r in rows if r[1] == "MAIN ST"]) == 4
    # ...and the distinct count agrees with the key that produced the rows.
    assert result.get("distinct_in_range", len(rows)) == len(rows)


def test_anchor_municipality_sorts_ahead_of_a_bbox_neighbour():
    result = geocode.geocode_address("Main St, San Francisco", limit=10)
    postcodes = [r["postcode"] for r in result["results"]]
    # Both San Francisco rows come before either Daly City row, even though
    # the Daly City ones are not the farthest from the anchor point.
    assert postcodes.index("94112") < postcodes.index("94014")


def test_a_doorway_with_many_units_reports_the_count_not_a_guess():
    """arg_min(unit, d) over 458 units at 1 Franklin St is a nondeterministic
    pick presented as a fact. Three units in the fixture make the same point."""
    result = geocode.geocode_address("1 Franklin Street, San Francisco")

    row = result["results"][0]
    assert row["number"] == "1"
    assert "unit" not in row
    assert row["unit_count"] == 3


def test_a_doorway_with_one_unit_still_names_it():
    result = geocode.geocode_address("3 Franklin Street, San Francisco")

    row = result["results"][0]
    assert row["unit"] == "Suite 900"
    assert "unit_count" not in row


# --- the anchor is never in another country (R28 HIGH-1, #229) -------------


def test_anchor_carries_its_country_and_admin_context():
    """Which "Cambridge" (or "London") an answer is about is not readable
    from a bare name, so the anchor always names its country and chain."""
    result = geocode.geocode_address("Market Street, San Francisco")

    assert result["anchor"]["country"] == "US"
    assert result["anchor"]["admin_context"] == ["United States", "California"]


def test_runner_up_anchor_never_crosses_a_border():
    """The fixture analogue of live "Baker Street, London": the top-ranked
    Cambridge (UK) has no boundary extent, and the only Cambridge that does
    is in Massachusetts. Answering with US doorways under a UK anchor is
    worse than answering nothing, so this is an honest empty."""
    result = geocode.geocode_address("Trumpington Street, Cambridge")

    assert result["results"] == []
    assert "anchor" not in result
    assert "no boundary extent" in result["note"]
    # ...and it says why the Massachusetts one was not used instead.
    assert "different country" in result["note"]
    assert "Cambridge (United States, US)" in result["note"]


def test_same_country_runner_up_anchor_is_used_and_named():
    """The case the fallback exists for: Springfield resolves to the MA one,
    which has no boundary; the IL one does, and is in the same country."""
    result = geocode.geocode_address("Main St, Springfield")

    assert result["anchor"]["id"] == "gers-div-springfield-il"
    assert result["anchor"]["admin_context"] == ["United States", "Illinois"]
    assert "the next candidate of that name in the same country" in result["note"]


def test_same_country_compares_iso_codes_then_the_admin_chain():
    us = {"country": "US", "admin_context": ["United States", "Illinois"]}
    gb = {"country": "GB", "admin_context": ["United Kingdom", "England"]}
    assert geocode._same_country(us, dict(us)) is True
    assert geocode._same_country(us, gb) is False
    # No code on either side -> the top of the admin chain decides.
    assert geocode._same_country(
        {"admin_context": ["Canada", "Ontario"]}, {"admin_context": ["Canada", "Quebec"]}
    ) is True
    # Nothing to compare is not a match: a places-fallback row carries
    # neither, and "unknown" must not read as "same".
    assert geocode._same_country({"country": None, "admin_context": []}, us) is False


# --- coverage --------------------------------------------------------------


def test_uncovered_country_says_no_data_not_no_street():
    result = geocode.geocode_address("High Street, Kensington")

    assert result["results"] == []
    assert result["anchor"]["name"] == "Kensington"
    assert "no Overture address coverage for United Kingdom (GB)" in result["note"]
    assert str(len(addresses.COVERED_COUNTRIES)) in result["note"]


def test_covered_country_with_no_such_street_says_gap_not_absence():
    result = geocode.geocode_address("Nonexistent Avenue, San Francisco")

    assert result["results"] == []
    assert "may be a gap in the data" in result["note"]


# --- server tool -----------------------------------------------------------


def test_tool_is_registered_and_budgeted():
    result = server.geocode_address("Market Street, San Francisco")
    assert result["results"]
    assert result["anchor"]["id"] == "gers-div-san-francisco"


def test_tool_clamps_limit():
    result = server.geocode_address("Market Street, San Francisco", limit=500)
    assert len(result["results"]) <= geocode.ADDRESS_MAX_LIMIT


def test_tool_is_in_the_search_profile():
    from placeroot import tool_profiles

    assert "geocode_address" in tool_profiles.PROFILES["search"]
