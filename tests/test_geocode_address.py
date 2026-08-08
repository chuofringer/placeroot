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
