"""Opt-in test against real Overture S3 data. Excluded by default (see
pyproject.toml addopts); run explicitly with `uv run pytest -m live`.
"""

import pytest

from placeroot import changes, db, geo, geocode, overture, routing


@pytest.mark.live
def test_find_places_against_real_overture_data():
    results = overture.find_places(47.6062, -122.3321, radius_m=500, limit=5)
    assert isinstance(results, list)
    assert len(results) <= 5
    for r in results:
        assert r["distance_m"] <= 500
        assert r["id"]  # GERS id (#25), real Overture data always carries one


@pytest.mark.live
def test_place_details_against_real_overture_data():
    hits = overture.find_places(47.6062, -122.3321, radius_m=500, limit=1)
    assert hits
    result = overture.place_details(id=hits[0]["id"])
    assert result is not None
    assert result["id"] == hits[0]["id"]


@pytest.mark.live
def test_within_distance_against_real_overture_data():
    result = overture.within_distance(47.6062, -122.3321, max_distance_m=500)
    assert isinstance(result["within"], bool)


@pytest.mark.live
def test_admin_lookup_against_real_overture_data():
    from placeroot import divisions

    result = divisions.admin_lookup(47.6062, -122.3321)
    assert isinstance(result["chain"], list)


@pytest.mark.live
def test_isochrone_against_real_overture_transportation_data():
    """Pike Place Market, Seattle: don't over-assert on shape, just that
    routing works end-to-end against a real transportation-theme extract.

    Note: nearest-node snapping can land on an isolated pedestrian-path
    fragment (a real gap in Overture's sidewalk connectivity, not a bug in
    this code) — pick a point known to sit on the connected street network
    rather than an arbitrary coordinate.
    """
    result = routing.isochrone(47.6097, -122.3422, minutes=10)
    assert result["stats"]["reachable_nodes"] > 1
    assert result["polygon"]["type"] == "Polygon"
    assert len(result["polygon"]["coordinates"][0]) >= 4


@pytest.mark.live
def test_resolve_place_against_real_overture_data():
    """#22 smoke test: a business name + city, with a rough location hint,
    should resolve to a place-kind candidate carrying a real GERS id.
    """
    results = geocode.resolve_place(
        "Manana coffee Austin", near_lat=30.2672, near_lon=-97.7431, limit=5
    )
    assert results
    assert any(r["kind"] == "place" and r["id"] for r in results)


@pytest.mark.live
def test_geocode_address_finds_an_exact_doorway():
    """#225: number + street + city -> the one point, live.

    "1600 Amphitheatre Parkway" is spelled "AMPHITHEATRE PKWY" in Overture's
    US address rows, so this only passes through the USPS suffix map.
    """
    result = geocode.geocode_address("1600 Amphitheatre Parkway, Mountain View")
    assert result["anchor"]["name"] == "Mountain View"
    assert len(result["results"]) == 1
    row = result["results"][0]
    assert row["number"] == "1600"
    assert row["street"].upper().startswith("AMPHITHEATRE")
    assert abs(row["lat"] - 37.4224) < 0.01
    assert abs(row["lon"] + 122.0842) < 0.01


@pytest.mark.live
def test_geocode_address_dedupes_a_whole_street():
    """#225: MARKET ST is ~3,000 raw address points over ~900 distinct
    number|street pairs; the answer must be the distinct ones, capped."""
    result = geocode.geocode_address("Market Street, San Francisco", limit=5)
    assert result["anchor"]["name"] == "San Francisco"
    assert len(result["results"]) == 5
    pairs = [(r["number"], r["street"]) for r in result["results"]]
    assert len(set(pairs)) == 5
    assert result["truncated"] is True
    assert result["distinct_in_range"] > 500


@pytest.mark.live
def test_geocode_address_folds_ordinals_to_nyc_spelling():
    """Task #23's original repro: NYC writes Fifth Avenue as "5 AVENUE", so
    "350 5th Ave" only resolves through the ordinal fold."""
    result = geocode.geocode_address("350 5th Ave, New York")
    assert result["results"], result.get("note")
    assert any(r["street"].upper().startswith("5 AVE") for r in result["results"])


@pytest.mark.live
def test_gers_ids_are_stable_enough_across_releases_for_diff_places_to_be_meaningful():
    """#376's precondition, checked against real data: diff_places's whole
    premise is that a place keeps the same GERS id release to release, so a
    disappearance/appearance is a real change and not id churn. This scans a
    tiny bbox in central Paris (dense with places, small enough to keep the
    live scan itself cheap) in both of the two live releases named in #376
    and asserts most ids that show up in one release also show up in the
    other.

    Deliberately not asserting on appeared/disappeared/changed *content* --
    that's what test_changes.py's offline fixtures cover. This is only the
    id-overlap sanity check the issue calls out as a precondition for
    trusting the tool at all: if it ever drops to <=0.5, the diff is mostly
    measuring id churn rather than real changes, and #309 should be told to
    stop rather than ship a misleading tool.

    LIMIT is a few hundred rows per side (see changes.SCAN_LIMIT's docstring
    for why the query-layer default is thousands; a live smoke test does not
    need anywhere near that to measure an overlap ratio) and the bbox is
    ~0.02 degrees square -- both kept small on purpose since this hits real
    S3 twice.
    """
    bbox = (2.34, 48.85, 2.36, 48.87)  # central Paris, ~0.02 deg square
    release_a, release_b = "2026-07-22.0", "2026-08-19.0"

    bbox_filter, bbox_params = geo.bbox_filter_sql(*bbox)
    ids_by_release = {}
    for release in (release_a, release_b):
        glob = overture.upstream_glob("places", "place", release=release)
        # ORDER BY id is load-bearing: this bbox holds far more than 300
        # places, and an unordered LIMIT takes an *arbitrary* 300 of them --
        # two arbitrary subsets of different releases can share nothing at
        # all even when the ids themselves are perfectly stable (measured:
        # 0.000 overlap unordered vs 0.875 ordered, 2026-08-21). Sorting by
        # id makes both sides take the same deterministic slice of the id
        # space, so the ratio measures id stability, not sampling luck.
        sql = f"""
            SELECT id FROM read_parquet('{glob}', hive_partitioning=1)
            WHERE {bbox_filter} AND id IS NOT NULL
            ORDER BY id
            LIMIT 300
        """
        with db.conn_lock:
            rows = db.shared_conn().execute(sql, dict(bbox_params)).fetchall()
        ids_by_release[release] = {r[0] for r in rows}

    ids_a, ids_b = ids_by_release[release_a], ids_by_release[release_b]
    assert ids_a and ids_b, "expected real place rows in both releases for this bbox"
    overlap_ratio = len(ids_a & ids_b) / len(ids_a | ids_b)
    print(
        f"\nGERS id overlap {release_a} vs {release_b} over {bbox}: "
        f"{overlap_ratio:.3f} ({len(ids_a & ids_b)} shared / {len(ids_a | ids_b)} total, "
        f"{len(ids_a)} in {release_a}, {len(ids_b)} in {release_b})"
    )
    assert overlap_ratio > 0.5, (
        f"GERS id overlap between {release_a} and {release_b} is only "
        f"{overlap_ratio:.3f} -- ids are not stable enough for diff_places "
        "to be meaningful; see #376/#309"
    )


@pytest.mark.live
def test_diff_places_end_to_end_against_real_overture_releases():
    """Smoke test for changes.diff_places itself (not just raw id overlap
    above) against the same tiny Paris bbox and the same two live releases
    -- proves the query layer actually runs against real S3 data, not just
    fixtures."""
    result = changes.diff_places(
        (2.34, 48.85, 2.36, 48.87), "2026-07-22.0", "2026-08-19.0", limit=20
    )
    assert result["releases"] == {"from": "2026-07-22.0", "to": "2026-08-19.0"}
    assert isinstance(result["counts"]["unchanged"], int)
    for bucket in ("appeared", "disappeared", "changed"):
        assert len(result[bucket]) <= 20


@pytest.mark.live
def test_comma_qualified_name_resolves_inside_its_qualifier():
    """#427: the observed defect, live. "Le Marais, Paris" used to fuzz
    across the comma and answer with three villages named "Le Mauvais Pas",
    none of them within 300 km of Paris. Assert on where the pin lands
    rather than on which feature wins it: the Marais is a place-theme name,
    and which of its rows ranks first is Overture's business, not ours.
    """
    resolved = geocode.resolve_named_place("Le Marais, Paris")
    assert resolved is not None
    assert geo.haversine_m(resolved["lat"], resolved["lon"], 48.8566, 2.3522) < 10_000


@pytest.mark.live
def test_ordinary_english_landmark_name_resolves():
    """#429: the observed defect, live. "Louvre Museum" derives no anchor —
    "Museum" is a feature noun and "Louvre" only prefix-matches the commune
    of Louvres — so geocode's places half was skipped and every compose on
    the shared resolver answered "no place matched".

    Assert on where the pin lands, not on which row wins it: Overture
    carries six businesses named exactly "Louvre Museum" (ticket resellers,
    scattered across the metro) alongside the Musée du Louvre itself, and
    which of them ranks first is a data question. Within 500 m of the
    pyramid is the answer being right.
    """
    resolved = geocode.resolve_named_place("Louvre Museum")
    assert resolved is not None
    assert geo.haversine_m(resolved["lat"], resolved["lon"], 48.8606, 2.3376) < 500


@pytest.mark.live
def test_half_matched_fuzzy_name_is_refused_rather_than_answered():
    """#431: the observed defect, live. "Gare du Nord" answered "Garen Du",
    a hamlet in Côtes-d'Armor, because _parse_region_suffix read the
    trailing "Nord" as Cameroon's CM-NO and the typo tier then scored a
    0.975 match against what was left of the query. A third of the string
    was never matched by anything, so the honest answer is no answer.

    Asserted as None rather than as "not Garen Du": whichever hamlet the
    current release happens to rank first, a fuzzy row that only covers
    part of the query is not this resolver's answer. The caller's
    not_found carries the try hint that asks for city/near context.

    The session is cleared first because #429's places leg reads it: this
    is the unpinned, no-city case the issue reports, and the next test is
    what the same query does once a city is known.
    """
    geocode.clear_resolve_session()
    assert geocode.resolve_named_place("Gare du Nord") is None


@pytest.mark.live
def test_refusing_the_hamlet_lets_the_places_leg_find_the_station():
    """#431 composed with #429, live. Refusing the fuzzy division is not
    only an honest miss — it is what lets the places leg run at all. That
    leg is gated on geocode having matched no division, and "Garen Du" was
    a division, so it stood in front of the search that can actually answer
    this. With a Paris session in hand, the same query now lands on the
    station itself.
    """
    geocode.clear_resolve_session()
    assert geocode.resolve_place("Louvre Museum", city="Paris")
    resolved = geocode.resolve_named_place("Gare du Nord")
    assert resolved is not None
    assert geo.haversine_m(resolved["lat"], resolved["lon"], 48.8809, 2.3553) < 1000


@pytest.mark.live
def test_real_typo_corrections_survive_the_refusal():
    """#431's other half, live: the #215 tier still corrects genuine
    misspellings, including the one whose typo token is unrecognizable on
    its own ("Sna"/"san" scores 0.556 as a lone word, so this pair passes
    only on whole-query similarity) and the "City, ST" form whose region
    suffix is legitimately consumed before scoring.
    """
    assert geocode.resolve_named_place("Sna Francisco")["name"] == "San Francisco"
    assert geocode.resolve_named_place("Berekley, CA")["name"] == "Berkeley"
