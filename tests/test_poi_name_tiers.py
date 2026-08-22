"""Tests for #373: typo/alt-spelling fallback tiers on POI name search.

find_places' name filter (and, through it, resolve_place's place-name
lookup — see geocode.resolve_place) used to be a plain ILIKE substring on
names.primary with no fallback at all: a misspelling or an alternate
spelling returned nothing. Three tiers now run in order, each only tried
when the previous one came back empty:

  1. names.primary ILIKE (unchanged, byte-identical when it finds anything)
  2. names.common alternate spellings, folded (mirrors geocode.py's #214)
  3. jaro_winkler fuzzy match on names.primary, over a bounded nearest-first
     pool inside the query's own bbox (mirrors geocode.py's #215)

Tier-2/3 rows carry "matched_by": "alt_name" | "fuzzy"; tier-1 rows never
do. The fixture (tests/fixtures/places.parquet) has a real name to fuzz —
"Blue Bottle Roastery", near CENTER_LAT/CENTER_LON — but no names.common
data at all (confirmed live: the `names` struct there is
STRUCT(primary VARCHAR), no "common" key), so the alt-name tier is tested
against a small tmp_path fixture built for that one purpose.
"""

import duckdb

from placeroot import geocode, overture, server

from .conftest import CENTER_LAT, CENTER_LON, FIXTURE_PATH

# --- tier 1: unchanged ------------------------------------------------------


def test_exact_match_carries_no_matched_by():
    """A literal ILIKE hit is byte-identical to pre-#373 behavior: no
    "matched_by" key at all, not even matched_by=None."""
    results = overture.find_places(CENTER_LAT, CENTER_LON, radius_m=1000, name="Roastery", limit=25)
    assert len(results) == 1
    assert results[0]["name"] == "Blue Bottle Roastery"
    assert "matched_by" not in results[0]


def test_exact_match_returns_the_literal_place_not_a_fuzzy_neighbor():
    """A query that matches something literally stands down the fallback
    tiers entirely (#373's "never both" rule for tier 1) — the answer is
    exactly the literal hit, not the closest fuzzy score in the area."""
    results = overture.find_places(
        CENTER_LAT, CENTER_LON, radius_m=1000, name="Blue Bottle Roastery", limit=25
    )
    assert len(results) == 1
    assert results[0]["name"] == "Blue Bottle Roastery"
    assert "matched_by" not in results[0]


# --- tier 3: fuzzy -----------------------------------------------------------


def test_typo_finds_real_place_via_fuzzy_tier():
    """A misspelling of the one real fixture name close enough to it
    (jaro_winkler ~0.95, well above the 0.92 threshold) returns it, tagged."""
    results = overture.find_places(
        CENTER_LAT, CENTER_LON, radius_m=1000, name="Blue Botle Roastery", limit=25
    )
    assert len(results) == 1
    assert results[0]["name"] == "Blue Bottle Roastery"
    assert results[0]["matched_by"] == "fuzzy"


def test_garbage_query_below_threshold_returns_empty_no_false_note():
    """A name with nothing close by similarity returns an honest empty
    list — no tier claims a match it doesn't have."""
    results = overture.find_places(
        CENTER_LAT, CENTER_LON, radius_m=1000, name="Xyzzyx Nonexistent Corp", limit=25
    )
    assert results == []
    payload = server.find_places(
        lat=CENTER_LAT, lon=CENTER_LON, radius_m=1000, name="Xyzzyx Nonexistent Corp"
    )
    assert payload["results"] == []
    assert "note" not in payload or "no exact match" not in payload.get("note", "")


def test_fuzzy_tier_is_bounded_to_the_query_bbox():
    """#373's core cost property: tier 3 only ever scores a pool drawn from
    the query's own bbox/radius, never the whole places theme. Searching
    for a misspelling of "Blue Bottle Roastery" (which only exists near
    CENTER_LAT/CENTER_LON) from a distant, unrelated bbox (the fixture's
    Arctic cluster) must not find it — proving the fuzzy pool did not reach
    across the dataset to pull in a candidate outside its own search area.
    """
    results = overture.find_places(78.0, 15.0, radius_m=1000, name="Blue Botle Roastery", limit=25)
    assert results == []


# --- server.py: corrective note ---------------------------------------------


def test_server_find_places_note_on_fuzzy_hit():
    payload = server.find_places(
        lat=CENTER_LAT, lon=CENTER_LON, radius_m=1000, name="Blue Botle Roastery"
    )
    assert payload["results"]
    assert payload["results"][0]["matched_by"] == "fuzzy"
    assert "note" in payload
    assert "Blue Bottle Roastery" in payload["note"]


def test_server_find_places_no_note_on_ordinary_match():
    payload = server.find_places(lat=CENTER_LAT, lon=CENTER_LON, radius_m=1000, name="Roastery")
    assert payload["results"]
    assert "matched_by" not in payload["results"][0]
    assert "note" not in payload


# --- resolve_place propagation -----------------------------------------------


def test_resolve_place_propagates_fuzzy_match_and_tag():
    """resolve_place's place-name lookup funnels through overture.find_places
    (geocode.resolve_place's ref_lat/ref_lon token loop) — a fuzzy hit there
    must come back labeled "fuzzy" (not silently dropped by the containment
    check _place_match_label would otherwise apply) and carry matched_by."""
    results = geocode.resolve_place(
        "Blue Botle Roastery", near_lat=CENTER_LAT, near_lon=CENTER_LON, limit=5
    )
    assert results
    top = results[0]
    assert top["kind"] == "place"
    assert top["name"] == "Blue Bottle Roastery"
    assert top["match"] == "fuzzy"
    assert top["matched_by"] == "fuzzy"


def test_resolve_place_note_on_fuzzy_hit():
    payload = server.resolve_place(
        "Blue Botle Roastery", near_lat=CENTER_LAT, near_lon=CENTER_LON, limit=5
    )
    assert payload["results"]
    assert payload["results"][0]["matched_by"] == "fuzzy"
    assert "note" in payload


# --- tier 2: names.common alternate spellings -------------------------------


def _build_alt_name_fixture(tmp_path, common_sql=None):
    """A minimal places.parquet-shaped fixture at CENTER_LAT/CENTER_LON,
    schema-identical to tests/fixtures/places.parquet except `names` also
    carries a `common` MAP(VARCHAR, VARCHAR) — the default fixture has no
    such field at all (confirmed live), so the alt-name tier needs its own
    fixture to exercise, same pattern as test_find_places.py's
    _build_wildcard_fixture.

    One row: primary name "Muenchen Coffee House" (an exonym-flavored
    fixture name unrelated by substring to its own alt spelling), with a
    names.common alternate "Munich Coffee House" — a query for the alt
    spelling must NOT literally match the primary name (tier 1 fails) but
    must match via tier 2. `common_sql` overrides the names.common MAP
    literal (#374: the dedup tests store the same alternate under several
    locale keys).
    """
    out = tmp_path / "alt_name_fixture.parquet"
    con = duckdb.connect()
    common_sql = common_sql or "MAP {'en': 'Munich Coffee House'}"
    con.execute(f"""
        COPY (
            SELECT
                'alt1'::VARCHAR AS id,
                {{'xmin': {CENTER_LON}, 'ymin': {CENTER_LAT},
                  'xmax': {CENTER_LON}, 'ymax': {CENTER_LAT}}}
                    ::STRUCT(xmin DOUBLE, ymin DOUBLE, xmax DOUBLE, ymax DOUBLE) AS bbox,
                {{'primary': 'Muenchen Coffee House',
                  'common': {common_sql}}}
                    ::STRUCT("primary" VARCHAR, common MAP(VARCHAR, VARCHAR)) AS names,
                {{'primary': 'coffee_shop', 'alternates': []}}
                    ::STRUCT("primary" VARCHAR, alternates VARCHAR[]) AS taxonomy,
                'coffee_shop'::VARCHAR AS basic_category,
                'open'::VARCHAR AS operating_status,
                0.9::DOUBLE AS confidence,
                CAST([] AS STRUCT(freeform VARCHAR, locality VARCHAR, region VARCHAR,
                                   postcode VARCHAR, country VARCHAR)[]) AS addresses,
                CAST([] AS VARCHAR[]) AS websites,
                CAST([] AS VARCHAR[]) AS phones,
                CAST([] AS VARCHAR[]) AS socials,
                CAST(NULL AS STRUCT("names" STRUCT("primary" VARCHAR))) AS brand,
                CAST([] AS STRUCT(dataset VARCHAR, record_id VARCHAR)[]) AS sources
        ) TO '{out}' (FORMAT PARQUET)
    """)
    return out


def test_alt_name_tier_matches_names_common_spelling(tmp_path):
    fixture = _build_alt_name_fixture(tmp_path)
    overture.set_data_path(str(fixture))
    try:
        results = overture.find_places(
            CENTER_LAT, CENTER_LON, radius_m=1000, name="Munich Coffee House", limit=25
        )
    finally:
        overture.set_data_path(str(FIXTURE_PATH))
    # Tier 1 alone would have failed: "Munich Coffee House" is not a
    # substring of the stored primary name "Muenchen Coffee House" — this
    # result only exists because tier 2 (names.common) found it.
    assert len(results) == 1
    assert results[0]["name"] == "Muenchen Coffee House"
    assert results[0]["matched_by"] == "alt_name"


def test_alt_name_tier_dedupes_same_spelling_across_locales(tmp_path):
    """#374: names.common is a per-language map and the same alternate
    routinely repeats across locale keys — a plain UNNEST(map_values(...))
    returned one duplicate row per locale for a single place id. Three
    locales carrying the same alternate (plus one different) must still
    yield exactly one row."""
    fixture = _build_alt_name_fixture(
        tmp_path,
        common_sql=(
            "MAP {'en': 'Munich Coffee House', 'es': 'Munich Coffee House', "
            "'fr': 'Munich Coffee House', 'de': 'Muenchener Kaffeehaus'}"
        ),
    )
    overture.set_data_path(str(fixture))
    try:
        results = overture.find_places(
            CENTER_LAT, CENTER_LON, radius_m=1000, name="Munich Coffee House", limit=25
        )
    finally:
        overture.set_data_path(str(FIXTURE_PATH))
    assert len(results) == 1
    assert results[0]["name"] == "Muenchen Coffee House"
    assert results[0]["matched_by"] == "alt_name"


def test_missing_names_common_column_skips_tier_2_without_error(tmp_path):
    """The default fixture's `names` struct has no "common" key at all — the
    alt-name tier's query fails to bind and is caught, falling through to
    tier 3 rather than raising. Proven here by a typo query against the
    ordinary (no names.common) fixture succeeding via the fuzzy tier
    instead of raising duckdb.Error."""
    results = overture.find_places(
        CENTER_LAT, CENTER_LON, radius_m=1000, name="Blue Botle Roastery", limit=25
    )
    assert results
    assert results[0]["matched_by"] == "fuzzy"


# --- #374: review fixes on the #373 tiers ------------------------------------


def _build_named_places_fixture(tmp_path, rows, filename="places_374.parquet"):
    """A places.parquet-shaped fixture (schema-identical to
    tests/fixtures/places.parquet: `names` is STRUCT(primary VARCHAR), no
    "common" key) holding `rows` of (id, name, lat, lon) — for the #374
    regression tests that need specific name/distance layouts."""
    out = tmp_path / filename
    con = duckdb.connect()
    selects = " UNION ALL ".join(
        f"""
        SELECT
            '{rid}'::VARCHAR AS id,
            {{'xmin': {lon}, 'ymin': {lat}, 'xmax': {lon}, 'ymax': {lat}}}
                ::STRUCT(xmin DOUBLE, ymin DOUBLE, xmax DOUBLE, ymax DOUBLE) AS bbox,
            {{'primary': '{name}'}}::STRUCT("primary" VARCHAR) AS names,
            {{'primary': 'coffee_shop', 'alternates': []}}
                ::STRUCT("primary" VARCHAR, alternates VARCHAR[]) AS taxonomy,
            'coffee_shop'::VARCHAR AS basic_category,
            'open'::VARCHAR AS operating_status,
            0.9::DOUBLE AS confidence,
            CAST([] AS STRUCT(freeform VARCHAR, locality VARCHAR, region VARCHAR,
                               postcode VARCHAR, country VARCHAR)[]) AS addresses,
            CAST([] AS VARCHAR[]) AS websites,
            CAST([] AS VARCHAR[]) AS phones,
            CAST([] AS VARCHAR[]) AS socials,
            CAST(NULL AS STRUCT("names" STRUCT("primary" VARCHAR))) AS brand,
            CAST([] AS STRUCT(dataset VARCHAR, record_id VARCHAR)[]) AS sources
        """
        for rid, name, lat, lon in rows
    )
    con.execute(f"COPY ({selects}) TO '{out}' (FORMAT PARQUET)")
    return out


def test_fuzzy_pool_scores_before_limiting_in_dense_areas(tmp_path, monkeypatch):
    """#374: the tier-3 pool used to be ORDER BY distance LIMIT N *before*
    any similarity test — in a dense area, N near-but-dissimilar storefronts
    filled the pool and the real match (slightly farther out) was never
    scored. The similarity predicate now gates admission to the pool, so a
    match beyond the N nearest rows still comes back. Modeled with a
    shrunken pool: 10 filler cafes at the center, the target ~1.1km out,
    pool capped at 5."""
    rows = [(f"fill{i}", f"Filler Cafe {i}", CENTER_LAT, CENTER_LON) for i in range(10)]
    rows.append(("target", "Empire State Building", CENTER_LAT + 0.01, CENTER_LON))
    fixture = _build_named_places_fixture(tmp_path, rows)
    monkeypatch.setattr(overture, "_POI_FUZZY_POOL_SIZE", 5)
    overture.set_data_path(str(fixture))
    try:
        results = overture.find_places(
            CENTER_LAT, CENTER_LON, radius_m=2000, name="Empire Statte Building", limit=25
        )
    finally:
        overture.set_data_path(str(FIXTURE_PATH))
    assert [r["name"] for r in results] == ["Empire State Building"]
    assert results[0]["matched_by"] == "fuzzy"


def test_fuzzy_matches_a_word_of_a_longer_stored_name(tmp_path):
    """#374: tier 1 is a substring match, so tier 3 must not hold a
    one-word query to whole-string similarity against a longer stored name:
    jaro_winkler('startbucks', 'starbucks coffee') is 0.89 — under the 0.92
    threshold — but against the name's prefix window of the query's length
    it is 0.96. The documented "Startbucks -> Starbucks" case, against a
    realistically-named row."""
    fixture = _build_named_places_fixture(
        tmp_path, [("sb1", "Starbucks Coffee", CENTER_LAT, CENTER_LON)]
    )
    overture.set_data_path(str(fixture))
    try:
        results = overture.find_places(
            CENTER_LAT, CENTER_LON, radius_m=1000, name="Startbucks", limit=25
        )
    finally:
        overture.set_data_path(str(FIXTURE_PATH))
    assert [r["name"] for r in results] == ["Starbucks Coffee"]
    assert results[0]["matched_by"] == "fuzzy"


def test_fuzzy_fallback_returns_nearest_first(tmp_path):
    """#374: tier 3 used to ORDER BY similarity DESC, so with limit=1 a
    far row whose spelling scored a shade higher beat a near one — breaking
    find_places' nearest-first contract. Similarity gates admission;
    distance orders. Both fixture names clear the threshold for the query,
    the farther one with the higher similarity — the nearer must win."""
    fixture = _build_named_places_fixture(tmp_path, [
        ("near", "Blue Bottle Roasters", CENTER_LAT, CENTER_LON),
        ("far", "Blue Bottle Roastery", CENTER_LAT + 0.005, CENTER_LON),
    ])
    overture.set_data_path(str(fixture))
    try:
        results = overture.find_places(
            CENTER_LAT, CENTER_LON, radius_m=2000, name="Blue Botle Roastery", limit=25
        )
        top_only = overture.find_places(
            CENTER_LAT, CENTER_LON, radius_m=2000, name="Blue Botle Roastery", limit=1
        )
    finally:
        overture.set_data_path(str(FIXTURE_PATH))
    assert [r["name"] for r in results] == ["Blue Bottle Roasters", "Blue Bottle Roastery"]
    assert [r["name"] for r in top_only] == ["Blue Bottle Roasters"]


def test_within_distance_does_not_leak_the_fuzzy_fallback():
    """#374: within_distance is a yes/no tool with no note surface — a
    misspelled name must stay an honest "no match", not silently become a
    claim about a differently-named place. The fixture's "Blue Bottle
    Roastery" is well within range and fuzzy-close to the query; it must
    still not be counted."""
    result = overture.within_distance(
        CENTER_LAT, CENTER_LON, 1000, name="Blue Botle Roastery"
    )
    assert result["within"] is False
    assert result["nearest"] is None
    assert result["distance_m"] is None
    # The literal spelling still works exactly as before.
    hit = overture.within_distance(
        CENTER_LAT, CENTER_LON, 1000, name="Blue Bottle Roastery"
    )
    assert hit["within"] is True


def test_resolve_place_gates_one_token_fuzzy_coincidence(tmp_path):
    """#374: resolve_place searches per token, so a one-token fuzzy
    coincidence — a bar named "King", typo-close to the token "kings" —
    used to be trusted as an answer to the whole three-token query "Kings
    Barbershop Springfield". A token-matched fallback row must cover the
    query's other distinctive words too, or be dropped."""
    fixture = _build_named_places_fixture(
        tmp_path, [("king1", "King", CENTER_LAT, CENTER_LON)]
    )
    overture.set_data_path(str(fixture))
    try:
        results = geocode.resolve_place(
            "Kings Barbershop Springfield", near_lat=CENTER_LAT, near_lon=CENTER_LON, limit=5
        )
    finally:
        overture.set_data_path(str(FIXTURE_PATH))
    assert not any(r.get("name") == "King" for r in results)


def test_resolve_place_note_reflects_post_budget_rows(monkeypatch):
    """#374: the fallback note used to be attached BEFORE budgeting, so it
    could name a row apply_budget then dropped from the answer. With every
    result budgeted away, no note may survive to describe a row the caller
    never sees."""
    def drop_everything(payload, key):
        payload = dict(payload)
        payload[key] = []
        payload["truncated"] = True
        return payload

    monkeypatch.setattr(server.budget, "apply_budget", drop_everything)
    payload = server.resolve_place(
        "Blue Botle Roastery", near_lat=CENTER_LAT, near_lon=CENTER_LON, limit=5
    )
    assert payload["results"] == []
    assert "note" not in payload
