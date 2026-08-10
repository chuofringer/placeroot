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

import duckdb
import pytest

from placeroot import addresses, geocode, overture, server


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
    needs no suffix transformation at all (verified live for DE/NL)."""
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
        # The five parse cases the sweep hit. A trailing number
        # after a leading street type is the street's own name...
        ("Calle 8", (None, "Calle 8")),
        ("Avenida 9", (None, "Avenida 9")),
        ("Carrera 7", (None, "Carrera 7")),
        # ...as is a leading number followed by a particle...
        ("8 de Octubre", (None, "8 de Octubre")),
        # ...while a digits-plus-letter token really is a house number.
        ("221B Baker Street", ("221B", "Baker Street")),
        ("Baker Street 221B", ("221B", "Baker Street")),
        # The German convention is the mirror image of Calle 8 -- the street
        # type is glued to the name, so the trailing number still splits.
        ("Hauptstraße 5", ("5", "Hauptstraße")),
        # The English numbered routes, which are the same grammar as
        # Calle 8 and are ordinary US address data -- the original rule was
        # Romance-only and split every one of these.
        ("Route 66", (None, "Route 66")),
        ("Highway 101", (None, "Highway 101")),
        ("Hwy 41", (None, "Hwy 41")),
        ("US 1", (None, "US 1")),
        ("Interstate 5", (None, "Interstate 5")),
        # ...including the ones whose type word is two tokens, where the
        # first token alone is far too ordinary to blocklist.
        ("County Road 12", (None, "County Road 12")),
        ("State Route 89", (None, "State Route 89")),
        ("Historic Route 66", (None, "Historic Route 66")),
        # ...and "State"/"County" on their own must keep splitting, which is
        # the whole reason the pairs are a separate list.
        ("State Street 12", ("12", "State Street")),
        ("County Line Road 40", ("40", "County Line Road")),
        # A real doorway on a numbered route is untouched: the leading-number
        # rule is checked first, so the house number still comes off the front.
        ("1234 Highway 101", ("1234", "Highway 101")),
        ("500 Route 66", ("500", "Route 66")),
    ],
)
def test_house_number_is_leading_or_trailing_only(text, expected):
    assert geocode._split_house_number(text) == expected


def test_route_66_searches_for_the_street_not_house_number_66():
    """The English half of the Calle 8 bug: "ROUTE 66" is the street's
    own name, and stripping the 66 searched Flagstaff for a street called
    "Route" -- an empty that looks like an honest one."""
    result = geocode.geocode_address("Route 66, Flagstaff", limit=10)

    assert result["anchor"]["name"] == "Flagstaff"
    assert {r["number"] for r in result["results"]} == {"2", "4", "6"}
    assert all(r["street"] == "ROUTE 66" for r in result["results"])


def test_a_real_doorway_on_a_numbered_route_still_resolves():
    """The rule above must not cost the addresses that genuinely sit on one:
    "500 Route 66" is house 500, not a street called "500 Route"."""
    result = geocode.geocode_address("4 Route 66, Flagstaff")

    assert _streets(result) == [("4", "ROUTE 66")]


def test_calle_8_searches_for_the_street_not_house_number_8():
    """Stripping the 8 searches Miami for a street called "Calle" and comes
    back empty -- an answer that looks honest and is not."""
    result = geocode.geocode_address("Calle 8, Miami", limit=10)

    assert result["anchor"]["name"] == "Miami"
    # Every doorway on the street, nearest Miami's own point first.
    assert {r["number"] for r in result["results"]} == {"1", "3", "5"}
    assert all(r["street"] == "CALLE 8" for r in result["results"])


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


# --- quadrants and prefix matching (#229) -----------------------


@pytest.mark.parametrize(
    "typed,reached",
    [
        ("Pennsylvania Avenue NW", "Pennsylvania Ave NW"),
        ("Pennsylvania Avenue Northwest", "Pennsylvania Ave NW"),
        ("K Street NE", "K St NE"),
        ("M Street SW", "M St SW"),
        ("Independence Avenue SE", "Independence Ave SE"),
    ],
)
def test_quadrant_suffixes_expand_both_ways(typed, reached):
    variants = [v.lower() for v in geocode._street_variants(typed)]
    assert reached.lower() in variants


def test_quadrant_street_is_found_end_to_end():
    result = geocode.geocode_address("1600 Pennsylvania Avenue NW, Washington")

    assert _streets(result) == [("1600", "PENNSYLVANIA AVE NW")]
    assert result["anchor"]["name"] == "Washington"


def test_a_street_without_its_quadrant_still_finds_the_quadrants():
    """The prefix match: a caller who leaves the quadrant off gets every
    street that carries one, as separate rows — not one merged answer that
    hides which of them it means."""
    result = geocode.geocode_address("Pennsylvania Avenue, Washington", limit=10)

    streets = {r["street"] for r in result["results"]}
    assert streets == {"PENNSYLVANIA AVE NW", "PENNSYLVANIA AVE SE"}
    assert len(result["results"]) == 3


def test_the_empty_note_does_not_overclaim_which_spellings_were_tried():
    result = geocode.geocode_address("Nonexistent Avenue, Washington")
    assert result["results"] == []
    assert "prefix" in result["note"]


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


# --- dedup does not merge municipalities (#229) ----------------


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


# --- the anchor is never in another country (#229) -------------


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


# --- the extent lookup memoizes facts, not failures ------------------


def _failing_conn(state, real):
    """overture.conn() whose *extent lookup* raises until state["fail"] is
    cleared. Scoped to that one query by its `division_id = $id` filter,
    because a connection that fails outright never reaches the anchor step --
    geocode() itself would raise first, which is a different bug.
    """
    class _Blip:
        def execute(self, sql, *a, **kw):
            if state["fail"] and "division_id = $id" in sql:
                raise duckdb.Error("transient upstream blip")
            return real().execute(sql, *a, **kw)

    return lambda: _Blip()


def test_a_transient_extent_lookup_failure_is_not_memoized(monkeypatch):
    """The 10.7s division_area lookup is memoized per process, and used to
    memoize its *errors* too. One blip therefore answered every later call
    for that city out of the cache -- no query, so no recovery -- and
    geocode_address rendered it as "Overture carries no boundary extent for
    San Francisco" until the process restarted."""
    monkeypatch.setattr(geocode, "_AREA_BBOX_CACHE", {})
    state = {"fail": True}
    monkeypatch.setattr(overture, "conn", _failing_conn(state, overture.conn))

    assert geocode._division_area_bbox("gers-div-san-francisco") is None

    state["fail"] = False
    assert geocode._division_area_bbox("gers-div-san-francisco") is not None


def test_a_transient_failure_does_not_strand_the_whole_tool(monkeypatch):
    """The same thing one level up: the blip must cost one empty answer, not
    every answer about that city for the life of the process."""
    monkeypatch.setattr(geocode, "_AREA_BBOX_CACHE", {})
    state = {"fail": True}
    monkeypatch.setattr(overture, "conn", _failing_conn(state, overture.conn))

    blipped = geocode.geocode_address("Market Street, San Francisco")
    assert blipped["results"] == []

    state["fail"] = False
    recovered = geocode.geocode_address("Market Street, San Francisco")
    assert recovered["results"], "the anchor stayed poisoned after upstream recovered"


def test_a_resolved_extent_is_still_memoized(monkeypatch):
    """...and the memoization it exists for still happens: the second call
    for a city whose extent resolved must not re-run the scan."""
    monkeypatch.setattr(geocode, "_AREA_BBOX_CACHE", {})
    calls = []
    real = geocode.overture.conn

    class _Counting:
        def execute(self, sql, *a, **kw):
            calls.append(sql)
            return real().execute(sql, *a, **kw)

    monkeypatch.setattr(overture, "conn", lambda: _Counting())
    assert geocode._division_area_bbox("gers-div-san-francisco") is not None
    assert len(calls) == 1
    assert geocode._division_area_bbox("gers-div-san-francisco") is not None
    assert len(calls) == 1, "the resolved extent was looked up twice"


def test_a_confirmed_absent_extent_is_still_memoized(monkeypatch):
    """A division with no area row is a fact about the dataset, not a
    failure, so it stays cached -- otherwise every "Brooklyn" query pays the
    full scan again to learn the same thing."""
    monkeypatch.setattr(geocode, "_AREA_BBOX_CACHE", {})
    calls = []
    real = geocode.overture.conn

    class _Counting:
        def execute(self, sql, *a, **kw):
            calls.append(sql)
            return real().execute(sql, *a, **kw)

    monkeypatch.setattr(overture, "conn", lambda: _Counting())
    assert geocode._division_area_bbox("gers-div-brooklyn") is None
    assert geocode._division_area_bbox("gers-div-brooklyn") is None
    assert len(calls) == 1


# --- an anchor too big to scan inside --------------------------------


def test_a_state_sized_anchor_is_refused_rather_than_scanned():
    """Texas's boundary is 13.1 x 10.7 degrees. That box blows past
    cache.MAX_TILES_PER_QUERY, so the tile cache declines it and the scan
    degrades to a direct read of the whole 474M-row addresses theme -- minutes
    of upstream work behind one tool call, answering with the Main Street of
    every town in the state."""
    result = geocode.geocode_address("Main Street, Texas")

    assert result["results"] == []
    assert "far larger than a city" in result["note"]
    # The note says how big, and does not claim Texas has no boundary.
    assert "13.1° x 10.7°" in result["note"]
    assert "no boundary extent" not in result["note"]
    assert "anchor" not in result


def test_the_size_guard_never_runs_the_scan(monkeypatch):
    """Pinned separately from the note: the whole point is the query that
    does not happen."""
    scans = []
    real = geocode._scan_addresses_in_bbox

    def counted(*a, **kw):
        scans.append(a)
        return real(*a, **kw)

    monkeypatch.setattr(geocode, "_scan_addresses_in_bbox", counted)
    geocode.geocode_address("Main Street, Texas")
    assert scans == []
    geocode.geocode_address("Market Street, San Francisco")
    assert len(scans) == 1, "a city-sized anchor must still be scanned"


def test_the_cap_admits_a_real_city_extent():
    """The guard has to sit above every genuine city. San Francisco's live
    boundary reaches the Farallon Islands 45 km offshore, which is the widest
    shape a real city takes."""
    assert not geocode._anchor_too_broad((-123.17, 37.70, -122.28, 37.83))
    assert geocode._anchor_too_broad((-106.65, 25.84, -93.51, 36.50))


# --- the runner-up loop rejects on country before paying for an extent -----


def test_a_cross_border_runner_up_is_rejected_before_its_extent_is_looked_up(monkeypatch):
    """The country test is a dict comparison; the extent lookup behind it is
    the 10.7s division_area scan. A cross-border candidate can never be the
    anchor whatever that scan returns, so looking it up first spent ~10s to
    reach a foregone conclusion."""
    looked_up = []
    real = geocode._anchor_bbox

    def spy(anchor_id, local_table):
        looked_up.append(anchor_id)
        return real(anchor_id, local_table)

    monkeypatch.setattr(geocode, "_anchor_bbox", spy)
    result = geocode.geocode_address("Trumpington Street, Cambridge")

    # Still the honest empty, with the Massachusetts candidate named...
    assert result["results"] == []
    assert "different country" in result["note"]
    assert "Cambridge (United States, US)" in result["note"]
    # ...but its extent was never fetched to find that out.
    assert "gers-div-cambridge-ma" not in looked_up


# --- a dataset with no number column says so -------------------------


def test_a_dataset_without_a_number_column_says_the_house_number_was_ignored(monkeypatch):
    """Dropping the filter silently answers a different question than the one
    asked: "1600 Amphitheatre Parkway" comes back as every doorway on the
    street. degraded_fields alone does not say that in words."""
    real = geocode._scan_addresses_in_bbox

    def without_number(*a, **kw):
        rows, distinct, matched, _ = real(*a, **kw)
        return rows, distinct, matched, False

    monkeypatch.setattr(geocode, "_scan_addresses_in_bbox", without_number)
    result = geocode.geocode_address("1600 Amphitheatre Parkway, Mountain View")

    assert result["results"]
    assert "no `number` column" in result["note"]
    assert "1600" in result["note"]


def test_the_dropped_number_note_stays_off_a_normal_answer():
    result = geocode.geocode_address("1600 Amphitheatre Parkway, Mountain View")
    assert "no `number` column" not in result.get("note", "")
