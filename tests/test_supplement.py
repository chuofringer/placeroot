"""The opt-in supplemental places layer (docs/SUPPLEMENT.md).

Two halves. The first covers the query-layer integration: with
`PLACEROOT_PLACES_SUPPLEMENT` pointing at a supplement file, every places
tool sees its rows unioned with Overture's; without it, results are
byte-identical to what they were before the feature existed — the layer is
opt-in, and "opt-in" has to be provable, not asserted.

The second covers scripts/build_supplement.py's pure functions: the OSM
tag -> Overture taxonomy mapping, the IMLS outlet mapping (visitable types
only, negative sentinels stripped, the truncated `LONGITUD` header), and the
name normalization the dedup pass matches on. Those run offline against
hand-written records; nothing here contacts Overpass or S3.
"""

import importlib.util
import sys
from pathlib import Path

import duckdb
import pytest

from placeroot import overture, resources, server

REPO_ROOT = Path(__file__).resolve().parent.parent
SUPPLEMENT_FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "places_supplement.parquet"

# scripts/ isn't a package — load by path, the same way test_overture_canary.py
# and test_bump_version.py do.
_spec = importlib.util.spec_from_file_location(
    "build_supplement", REPO_ROOT / "scripts" / "build_supplement.py"
)
build_supplement = importlib.util.module_from_spec(_spec)
# Registered before exec: @dataclass resolves annotations through
# sys.modules[cls.__module__], which is absent for a module loaded purely by
# path — the builder's OsmCategory would fail to define.
sys.modules[_spec.name] = build_supplement
_spec.loader.exec_module(build_supplement)

CENTER_LAT = 40.700000
CENTER_LON = -73.900000


@pytest.fixture
def supplement():
    """Turn the layer on for one test, off again afterwards."""
    overture.set_supplement_path(str(SUPPLEMENT_FIXTURE_PATH))
    try:
        yield SUPPLEMENT_FIXTURE_PATH
    finally:
        overture.set_supplement_path(None)


# --- query-layer integration ------------------------------------------------


def test_find_places_unions_supplement_rows_with_overture(supplement):
    """One result set, both datasets — supplement rows are added, not swapped in."""
    rows = overture.find_places(
        CENTER_LAT, CENTER_LON, radius_m=1000, name="Park", limit=25
    )
    names = {r["name"] for r in rows}
    assert {"Foxglove Park", "Harbor Green Park"} <= names, "no supplement rows"

    ids = {r["id"] for r in overture.find_places(
        CENTER_LAT, CENTER_LON, radius_m=1000, limit=25
    )}
    assert any(not i.startswith(("osm:", "imls:")) for i in ids), "no Overture rows"
    # Ranked together on one distance scale, not concatenated.
    distances = [r["distance_m"] for r in rows]
    assert distances == sorted(distances)


def test_category_filter_matches_a_supplement_category(supplement):
    rows = overture.find_places(
        CENTER_LAT, CENTER_LON, radius_m=1000, category="playground", limit=25
    )
    assert rows, "no playgrounds — the supplement's own category is missing"
    assert {r["name"] for r in rows} >= {
        "Riverbend Playground", "Maple Street Playground", "Cascade Splash Pad",
    }
    assert all(r["category"] == "playground" for r in rows)
    # basic_category is the taxonomy's top-level branch, from the same
    # bundled CSV search_categories answers from.
    assert all(r["basic_category"] == "active_life" for r in rows)


def test_alternates_reach_the_splash_pad_through_its_second_category(supplement):
    """A splash pad is a playground that a caller looks for as a water park.

    taxonomy.alternates is what find_places' category filter matches
    exactly, so both slugs find it — the reason the builder maps splash pads
    before plain playgrounds.
    """
    rows = overture.find_places(
        CENTER_LAT, CENTER_LON, radius_m=1000, category="water_park", limit=25
    )
    names = {r["name"] for r in rows}
    assert "Cascade Splash Pad" in names
    assert "Wave Lagoon Water Park" in names


def test_supplement_rows_carry_their_own_confidence(supplement):
    rows = overture.find_places(
        CENTER_LAT, CENTER_LON, radius_m=1000, category="library", limit=25
    )
    by_name = {r["name"]: r for r in rows}
    assert by_name["Metropolis Central Library"]["confidence"] == 0.7
    assert by_name["Volunteer Reading Room"]["confidence"] == 0.5


def test_place_details_resolves_an_osm_id(supplement):
    result = overture.place_details(id="osm:node/100000")

    assert result is not None
    assert result["name"] == "Riverbend Playground"
    assert result["category"] == "playground"
    # Per-row provenance: how a consumer tells a supplement row from an
    # Overture one and honors the right license.
    assert result["sources"] == [{"dataset": "OpenStreetMap", "record_id": "node/100000"}]


def test_place_details_resolves_an_imls_id(supplement):
    result = overture.place_details(id="imls:NY0042-000")

    assert result is not None
    assert result["name"] == "Metropolis Central Library"
    assert result["sources"][0]["dataset"] == "IMLS Public Libraries Survey"
    # The PLS "-3" sentinel in PHONE never becomes a phone number.
    assert not result["phones"]


def test_summarize_area_counts_supplement_rows(supplement):
    overture.set_supplement_path(None)
    baseline = overture.summarize_area(CENTER_LAT, CENTER_LON, radius_m=1000)
    overture.set_supplement_path(str(SUPPLEMENT_FIXTURE_PATH))
    summary = overture.summarize_area(CENTER_LAT, CENTER_LON, radius_m=1000)

    assert summary["total_places"] == baseline["total_places"] + 20
    categories = {row["category"] for row in summary["top_categories"]}
    assert "active_life" in categories


def test_tiles_cache_upstream_only_and_the_supplement_rides_on_top(
    supplement, tmp_path, monkeypatch
):
    """The supplement is appended *after* cache resolution.

    A tile is a verbatim copy of Overture for a 1-degree cell; if supplement
    rows leaked into one, turning the layer off would leave them behind and
    the cache would no longer be a faithful mirror of the release.
    """
    monkeypatch.setenv("PLACEROOT_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("PLACEROOT_CACHE", raising=False)
    monkeypatch.setenv("PLACEROOT_CACHE_SYNC", "1")

    rows = overture.find_places(
        CENTER_LAT, CENTER_LON, radius_m=1000, category="playground", limit=25
    )
    assert "Riverbend Playground" in {r["name"] for r in rows}

    tiles = list((tmp_path / "cache").rglob("*.parquet"))
    assert tiles, "the query should have materialized a tile"
    con = duckdb.connect()
    joined = ", ".join(f"'{p}'" for p in tiles)
    (leaked,) = con.execute(
        f"SELECT count(*) FROM read_parquet([{joined}]) WHERE id LIKE 'osm:%' OR id LIKE 'imls:%'"
    ).fetchone()
    assert leaked == 0


def test_without_the_supplement_nothing_changes():
    """The opt-in half of "opt-in": unset, the query layer is Overture-only."""
    assert overture.supplement_path() is None
    rows = overture.find_places(CENTER_LAT, CENTER_LON, radius_m=1000, limit=25)
    assert all(not r["id"].startswith(("osm:", "imls:")) for r in rows)
    assert overture.place_details(id="osm:node/100000") is None
    assert not overture.find_places(
        CENTER_LAT, CENTER_LON, radius_m=1000, category="playground", limit=25
    )


def test_env_var_enables_the_layer(monkeypatch):
    """The operator-facing switch, not just the test hook."""
    monkeypatch.setenv(overture.SUPPLEMENT_ENV_VAR, str(SUPPLEMENT_FIXTURE_PATH))
    assert overture.supplement_path() == str(SUPPLEMENT_FIXTURE_PATH)
    rows = overture.find_places(
        CENTER_LAT, CENTER_LON, radius_m=1000, category="campground", limit=25
    )
    assert {r["name"] for r in rows} == {"Cedar Hollow Campground", "Lakeside Caravan Park"}


def test_a_missing_supplement_fails_loudly(tmp_path):
    """Not "quietly answer Overture-only": an operator who set the variable
    is expecting those rows, and a silently absent layer is indistinguishable
    from an area with no playgrounds in it."""
    missing = tmp_path / "not-built-yet.parquet"
    overture.set_supplement_path(str(missing))
    try:
        with pytest.raises(overture.UpstreamUnavailable) as excinfo:
            overture.find_places(CENTER_LAT, CENTER_LON, radius_m=1000)
    finally:
        overture.set_supplement_path(None)

    detail = str(excinfo.value)
    assert str(missing) in detail
    assert overture.SUPPLEMENT_ENV_VAR in detail
    assert "build_supplement" in detail


def test_the_missing_supplement_error_reaches_the_tool_as_a_structured_error(tmp_path):
    overture.set_supplement_path(str(tmp_path / "nope.parquet"))
    try:
        result = server.find_places(CENTER_LAT, CENTER_LON, radius_m=1000)
    finally:
        overture.set_supplement_path(None)
    assert result["error"] == "upstream_unavailable"


def test_data_version_reports_the_active_supplement(supplement):
    payload = resources.data_version_payload()

    assert payload["supplement"]["path"] == str(SUPPLEMENT_FIXTURE_PATH)
    # The committed fixture ships no sidecar (it is built by
    # scripts/build_fixture.py, not by a real Overpass run), so the payload
    # says so rather than implying counts it doesn't have.
    assert "row counts unknown" in payload["supplement"]["note"]
    # The tool and the resource still share one code path (test_resources.py
    # asserts equality; this pins that the supplement rides along).
    assert server.data_version() == payload


def test_data_version_reports_sidecar_metadata_when_it_is_there(tmp_path):
    supplement_path = tmp_path / "supp.parquet"
    supplement_path.write_bytes(b"")  # existence is all the payload reads
    Path(f"{supplement_path}.meta.json").write_text(
        '{"built_at": "2026-08-09T00:00:00+00:00", "rows": 20, '
        '"per_source": {"OpenStreetMap": 18}, "per_category": {"playground": 4}, '
        '"bboxes": [[-74, 40, -73, 41]]}',
        encoding="utf-8",
    )
    overture.set_supplement_path(str(supplement_path))
    try:
        block = resources.data_version_payload()["supplement"]
    finally:
        overture.set_supplement_path(None)

    assert block["rows"] == 20
    assert block["per_source"] == {"OpenStreetMap": 18}
    assert block["built_at"] == "2026-08-09T00:00:00+00:00"
    # Operator detail that would cost context without changing an answer.
    assert "bboxes" not in block


def test_data_version_says_nothing_when_the_layer_is_off():
    assert "supplement" not in resources.data_version_payload()


def test_other_themes_never_see_the_supplement(supplement):
    """_with_supplement is places-only; unioning places rows into a
    divisions scan would be a silent data-corruption bug."""
    source = overture._with_supplement("read_parquet('x')", theme="divisions")
    assert source == "read_parquet('x')"
    assert "UNION ALL BY NAME" in overture._with_supplement("read_parquet('x')")


# --- builder: OSM tag -> Overture taxonomy ----------------------------------


@pytest.mark.parametrize(
    ("tags", "expected_primary", "expected_alternates"),
    [
        ({"leisure": "playground"}, "playground", ()),
        ({"leisure": "playground", "playground": "splash_pad"}, "playground", ("water_park",)),
        ({"playground:water": "yes"}, "playground", ("water_park",)),
        ({"amenity": "splash_pad"}, "playground", ("water_park",)),
        ({"leisure": "water_park"}, "water_park", ()),
        ({"leisure": "park"}, "park", ()),
        ({"tourism": "museum"}, "museum", ()),
        ({"amenity": "library"}, "library", ()),
        ({"tourism": "zoo"}, "zoo", ()),
        ({"tourism": "aquarium"}, "aquarium", ()),
        ({"natural": "beach"}, "beach", ()),
        ({"information": "trailhead"}, "hiking_trail", ("trail",)),
        ({"tourism": "camp_site"}, "campground", ()),
        ({"tourism": "caravan_site"}, "campground", ()),
    ],
)
def test_osm_tags_map_to_the_documented_taxonomy(tags, expected_primary, expected_alternates):
    category = build_supplement.classify(tags)
    assert category is not None
    assert category.taxonomy == expected_primary
    assert category.alternates == expected_alternates


def test_a_splash_pad_is_classified_before_the_plain_playground_rule():
    """Both rules match a splash pad's tags; the specific one has to win, or
    the row loses the water_park alternate that makes it findable."""
    tags = {"leisure": "playground", "playground": "splash_pad"}
    assert build_supplement.classify(tags).name == "splash_pad"


def test_unmapped_tags_are_not_invented():
    assert build_supplement.classify({"amenity": "restaurant"}) is None
    assert build_supplement.classify({}) is None


def test_every_taxonomy_slug_exists_in_the_bundled_overture_csv():
    """A slug that isn't in the taxonomy is a filter nobody can guess, and a
    basic_category of None."""
    for category in build_supplement.CATEGORIES:
        for slug in (category.taxonomy, *category.alternates):
            assert build_supplement.basic_category_for(slug), f"{slug} is not an Overture slug"


def test_osm_row_carries_ids_contacts_and_address():
    row = build_supplement.osm_row({
        "type": "way", "id": 42, "center": {"lat": 40.7, "lon": -73.9},
        "tags": {
            "leisure": "park", "name": "Elm Park", "website": "https://elm.example",
            "phone": "+1-555-0100", "addr:housenumber": "12", "addr:street": "Elm St",
            "addr:city": "Metropolis",
        },
    })
    assert row[0] == "osm:way/42"
    assert row[2] == {"primary": "Elm Park"}
    assert row[6] == build_supplement.OSM_CONFIDENCE
    assert row[7] == [{
        "freeform": "12 Elm St", "locality": "Metropolis",
        "region": None, "postcode": None, "country": None,
    }]
    assert row[8] == ["https://elm.example"]
    assert row[9] == ["+1-555-0100"]
    assert row[12] == [{"dataset": "OpenStreetMap", "record_id": "way/42"}]
    # bbox is a degenerate point box: bbox.ymin/xmin is how the query layer
    # reads a place's coordinates.
    assert row[1] == {"xmin": -73.9, "ymin": 40.7, "xmax": -73.9, "ymax": 40.7}


def test_a_way_without_a_center_has_no_coordinate_to_use():
    assert build_supplement.osm_row(
        {"type": "way", "id": 7, "tags": {"leisure": "park", "name": "Nowhere Park"}}
    ) is None


@pytest.mark.parametrize(
    ("tags", "kept"),
    [
        ({"leisure": "playground"}, True),
        ({"natural": "beach"}, True),
        ({"information": "trailhead"}, True),
        ({"playground:water": "yes"}, True),
        ({"tourism": "museum"}, False),
        ({"amenity": "library"}, False),
        ({"leisure": "park"}, False),
    ],
)
def test_unnamed_elements_survive_only_where_being_unnamed_is_normal(tags, kept):
    row = build_supplement.osm_row(
        {"type": "node", "id": 1, "lat": 40.7, "lon": -73.9, "tags": tags}
    )
    assert (row is not None) is kept
    if kept:
        assert row[2] == {"primary": None}


# --- builder: IMLS Public Libraries Survey ----------------------------------


def _pls(**overrides) -> dict:
    record = {
        "FSCSKEY": "NY0042", "FSCS_SEQ": "003", "C_OUT_TY": "BR",
        "LIBNAME": "Elmwood Branch", "ADDRESS": "1 Elm St", "CITY": "Metropolis",
        "STABR": "NY", "ZIP": "10001", "PHONE": "+1-555-0100",
        "LATITUDE": "40.7", "LONGITUD": "-73.9",
    }
    record.update(overrides)
    return record


@pytest.mark.parametrize(("outlet_type", "kept"), [
    ("CE", True), ("BR", True), ("BS", False), ("BM", False), ("", False),
])
def test_only_visitable_outlet_types_become_places(outlet_type, kept):
    """A bookmobile's coordinate is a depot and a books-by-mail outlet's is a
    mailroom; neither is somewhere to go."""
    assert (build_supplement.imls_row(_pls(C_OUT_TY=outlet_type)) is not None) is kept


def test_the_longitude_column_is_the_truncated_pls_header():
    """PLS truncates headers to 8 characters: reading LONGITUDE finds nothing
    and every library silently lands on the prime meridian."""
    row = build_supplement.imls_row(_pls())
    assert row[1]["xmin"] == -73.9
    assert build_supplement.imls_row(
        {**_pls(), "LONGITUD": None, "LONGITUDE": "-73.9"}
    ) is None


@pytest.mark.parametrize("sentinel", ["-1", "-3", "-4"])
def test_negative_sentinels_never_reach_the_output(sentinel):
    row = build_supplement.imls_row(_pls(PHONE=sentinel, ADDRESS=sentinel, ZIP=sentinel))
    assert row[9] == []
    assert row[7][0]["freeform"] is None
    assert row[7][0]["postcode"] is None
    # And a sentinel in a coordinate drops the row rather than placing it in
    # the Atlantic.
    assert build_supplement.imls_row(_pls(LATITUDE=sentinel)) is None
    assert build_supplement.imls_row(_pls(LONGITUD=f"{sentinel}.0")) is None


def test_imls_rows_are_libraries_with_their_own_id_and_provenance():
    row = build_supplement.imls_row(_pls())
    assert row[0] == "imls:NY0042-003"
    assert row[3] == {"primary": "library", "alternates": []}
    assert row[6] == build_supplement.IMLS_CONFIDENCE
    assert row[12] == [
        {"dataset": "IMLS Public Libraries Survey", "record_id": "NY0042-003"}
    ]


def test_read_imls_csv_filters_to_the_requested_bboxes(tmp_path):
    csv_path = tmp_path / "outlets.csv"
    header = "FSCSKEY,FSCS_SEQ,C_OUT_TY,LIBNAME,ADDRESS,CITY,STABR,ZIP,PHONE,LATITUDE,LONGITUD"
    csv_path.write_text(
        f"{header}\n"
        "NY0042,000,CE,Inside Library,1 Elm St,Metropolis,NY,10001,-3,40.70,-73.90\n"
        "CA0001,000,CE,Outside Library,2 Oak St,Faraway,CA,90001,-3,34.05,-118.24\n"
        "NY0042,001,BS,Bookmobile,3 Ash St,Metropolis,NY,10001,-3,40.70,-73.90\n",
        encoding="utf-8",
    )
    rows = build_supplement.read_imls_csv(csv_path, [(-74.0, 40.6, -73.8, 40.8)])
    assert [r[2]["primary"] for r in rows] == ["Inside Library"]

    everywhere = build_supplement.read_imls_csv(csv_path, [])
    assert {r[2]["primary"] for r in everywhere} == {"Inside Library", "Outside Library"}


# --- builder: dedup ---------------------------------------------------------


@pytest.mark.parametrize(("a", "b"), [
    ("Riverside Park", "riverside park"),
    ("St. Mary's Library", "St Marys Library"),
    ("Café Beach", "Cafe Beach"),
    ("Elm  Park ", "elm park"),
])
def test_names_that_should_match_normalize_identically(a, b):
    assert build_supplement.normalize_name(a) == build_supplement.normalize_name(b)


def test_normalization_does_not_collapse_different_places():
    assert build_supplement.normalize_name("North Park") != build_supplement.normalize_name(
        "South Park"
    )
    assert build_supplement.normalize_name(None) == ""


def test_dedup_drops_a_nearby_same_name_match_but_keeps_the_rest(monkeypatch):
    rows = [
        build_supplement.osm_row({
            "type": "node", "id": 1, "lat": 40.7, "lon": -73.9,
            "tags": {"leisure": "park", "name": "Riverside Park"},
        }),
        # Same name, ~1.1km away: a different park, not a duplicate.
        build_supplement.osm_row({
            "type": "node", "id": 2, "lat": 40.71, "lon": -73.9,
            "tags": {"leisure": "park", "name": "Riverside Park"},
        }),
        # Unnamed: nothing to match on, and the categories Overture is
        # thinnest on are exactly the ones that arrive unnamed.
        build_supplement.osm_row({
            "type": "node", "id": 3, "lat": 40.7, "lon": -73.9,
            "tags": {"leisure": "playground"},
        }),
    ]
    monkeypatch.setattr(
        build_supplement, "overture_names_in_bbox",
        lambda con, glob, bbox: [("riverside park", 40.70005, -73.90005)],
    )

    kept, dropped = build_supplement.dedup_against_overture(
        rows, None, "glob", [(-74.0, 40.6, -73.8, 40.8)]
    )

    assert [r[0] for r in kept] == ["osm:node/2", "osm:node/3"]
    assert dropped == {"park": 1}


def test_summary_counts_by_source_and_category():
    rows = [
        build_supplement.osm_row({
            "type": "node", "id": 1, "lat": 40.7, "lon": -73.9,
            "tags": {"leisure": "park", "name": "A Park"},
        }),
        build_supplement.imls_row(_pls()),
    ]
    summary = build_supplement.summarize(rows, {"beach": 2})
    assert summary.per_source == {"OpenStreetMap": 1, "IMLS Public Libraries Survey": 1}
    assert summary.per_category_kept == {"park": 1, "library": 1}
    assert summary.per_category_dropped == {"beach": 2}


# --- builder: CLI argument parsing -----------------------------------------


def test_bbox_is_parsed_as_minlon_minlat_maxlon_maxlat():
    assert build_supplement.parse_bbox("-74.05,40.60,-73.85,40.85") == (
        -74.05, 40.60, -73.85, 40.85
    )


@pytest.mark.parametrize("bad", ["1,2,3", "a,b,c,d", "-73.8,40.6,-74.0,40.8"])
def test_a_bbox_that_cannot_be_a_box_is_rejected_at_parse_time(bad):
    import argparse

    with pytest.raises(argparse.ArgumentTypeError):
        build_supplement.parse_bbox(bad)


def test_an_unknown_category_names_the_valid_ones():
    import argparse

    with pytest.raises(argparse.ArgumentTypeError) as excinfo:
        build_supplement.parse_categories("playground,skatepark")
    assert "skatepark" in str(excinfo.value)
    assert "playground" in str(excinfo.value)


def test_overpass_query_asks_for_nodes_and_ways_with_a_center():
    query = build_supplement.overpass_query(
        build_supplement.CATEGORIES_BY_NAME["playground"], (-74.0, 40.6, -73.8, 40.8)
    )
    assert 'node["leisure"="playground"](40.6,-74.0,40.8,-73.8);' in query
    assert 'way["leisure"="playground"](40.6,-74.0,40.8,-73.8);' in query
    # `out center` is what gives a way (a playground is a polygon in OSM) a
    # single coordinate to store.
    assert query.strip().endswith("out center tags;")
