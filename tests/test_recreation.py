"""The opt-in recreation layer: places queries additionally reading Overture's
base theme (recreation.py, docs/RECREATION.md).

Everything here runs offline against synthetic base-theme fixtures built in
this module with duckdb — the same WKT-free, bbox-and-columns pattern
test_land_use.py uses, since these are the only tests that need theme=base
rows shaped for a places union. Nothing contacts S3.

The layer is off unless a test turns it on, which is also the point of
test_layer_off_by_default: the default path must stay exactly the
single-theme query it was.
"""

import duckdb
import pytest

from placeroot import overture, recreation, resources, server

from .conftest import CENTER_LAT, CENTER_LON

# Roughly 111 m per 0.001 degrees of latitude at this latitude; the fixture
# boxes below are sized and placed in those units so the distances the
# assertions check are obvious from the coordinates.
_DEG = 0.001


def _bbox(lat_min, lat_max, lon_min, lon_max):
    return {"xmin": lon_min, "ymin": lat_min, "xmax": lon_max, "ymax": lat_max}


# (id, bbox, subtype, class, name). Boxes are deliberately *not* squares
# centred on their nominal position: the centroid collapse is what the
# distance assertions exercise, so a row whose south-west corner and centre
# differ is the interesting case.
_LAND_USE_ROWS = [
    # Centre at (CENTER_LAT, CENTER_LON + 0.002) — ~169 m east of centre.
    ("lu-play-named", _bbox(CENTER_LAT - _DEG, CENTER_LAT + _DEG,
                            CENTER_LON + _DEG, CENTER_LON + 3 * _DEG),
     "recreation", "playground", "Riverside Playground"),
    # The row the whole layer exists for: a real playground with no name.
    ("lu-play-unnamed", _bbox(CENTER_LAT, CENTER_LAT + 2 * _DEG,
                              CENTER_LON, CENTER_LON + 2 * _DEG),
     "recreation", "playground", None),
    ("lu-park", _bbox(CENTER_LAT - 2 * _DEG, CENTER_LAT, CENTER_LON - 2 * _DEG, CENTER_LON),
     "park", "park", "Test Park"),
    ("lu-dog-park", _bbox(CENTER_LAT, CENTER_LAT + _DEG, CENTER_LON, CENTER_LON + _DEG),
     "park", "dog_park", "Test Dog Run"),
    ("lu-reserve", _bbox(CENTER_LAT, CENTER_LAT + _DEG, CENTER_LON - _DEG, CENTER_LON),
     "protected", "state_park", "Test State Park"),
    # Excluded by the class map: a pitch is part of a facility, not a
    # destination, and there are thousands of them in a city.
    ("lu-pitch", _bbox(CENTER_LAT, CENTER_LAT + _DEG, CENTER_LON, CENTER_LON + _DEG),
     "recreation", "pitch", "Excluded Pitch"),
    ("lu-garden", _bbox(CENTER_LAT, CENTER_LAT + _DEG, CENTER_LON, CENTER_LON + _DEG),
     "horticulture", "garden", "Excluded Garden"),
]

_LAND_ROWS = [
    ("land-beach", _bbox(CENTER_LAT, CENTER_LAT + _DEG, CENTER_LON, CENTER_LON + _DEG),
     "sand", "beach", "Test Beach"),
    ("land-tree", _bbox(CENTER_LAT, CENTER_LAT + _DEG, CENTER_LON, CENTER_LON + _DEG),
     "wood", "tree", None),
]


def _write_base_fixture(path, rows, *, include_class=True, include_bbox=True) -> None:
    """One base-theme parquet in the shape recreation.py projects from.

    include_class/include_bbox drop an essential column so the schema-drift
    path (branch dropped, places query still answers) can be exercised.
    """
    con = duckdb.connect()
    cols = ["id VARCHAR"]
    if include_bbox:
        cols.append("bbox STRUCT(xmin DOUBLE, ymin DOUBLE, xmax DOUBLE, ymax DOUBLE)")
    cols.append("subtype VARCHAR")
    if include_class:
        cols.append("class VARCHAR")
    cols.append('names STRUCT("primary" VARCHAR)')
    cols.append("sources STRUCT(dataset VARCHAR, record_id VARCHAR)[]")
    con.execute(f"CREATE TABLE base ({', '.join(cols)})")
    for id_, bbox, subtype, class_, name in rows:
        values = [id_]
        if include_bbox:
            values.append(bbox)
        values.append(subtype)
        if include_class:
            values.append(class_)
        values.append({"primary": name})
        values.append([{"dataset": "OpenStreetMap", "record_id": id_}])
        placeholders = ", ".join("?" for _ in values)
        con.execute(f"INSERT INTO base VALUES ({placeholders})", values)
    con.execute(f"COPY base TO '{path}' (FORMAT PARQUET)")
    con.close()


@pytest.fixture
def base_fixtures(tmp_path):
    """Point theme=base's two types at synthetic fixtures. Layer still off."""
    land_use = tmp_path / "land_use.parquet"
    land = tmp_path / "land.parquet"
    _write_base_fixture(land_use, _LAND_USE_ROWS)
    _write_base_fixture(land, _LAND_ROWS)
    overture.set_data_path(str(land_use), theme="base", type_="land_use")
    overture.set_data_path(str(land), theme="base", type_="land")
    try:
        yield tmp_path
    finally:
        overture.set_data_path(None, theme="base", type_="land_use")
        overture.set_data_path(None, theme="base", type_="land")


@pytest.fixture
def layer_on(base_fixtures):
    """Base fixtures in place *and* the layer switched on."""
    recreation.set_enabled(True)
    try:
        yield base_fixtures
    finally:
        recreation.set_enabled(None)


def _names(rows):
    return {r["name"] for r in rows}


def _ids(rows):
    return {r["id"] for r in rows}


# --- the default path is untouched -----------------------------------------


def test_layer_can_be_switched_off(base_fixtures):
    """Fixtures present but the layer off: results are places-only.

    The suite pins it off (see conftest.recreation_layer_off), which is what
    every other test module's ground-truth counts depend on.
    """
    assert recreation.enabled() is False
    rows = overture.find_places(CENTER_LAT, CENTER_LON, radius_m=1000, limit=25)
    assert not _ids(rows) & {"lu-play-named", "lu-park", "land-beach"}
    assert overture.find_places(CENTER_LAT, CENTER_LON, category="playground") == []


def test_union_branches_empty_when_disabled(base_fixtures):
    assert recreation.union_branches((-74.0, 40.0, -73.0, 41.0)) == []


def test_the_layer_is_on_by_default(monkeypatch):
    """The variable is an opt-out: unset means on."""
    monkeypatch.delenv(recreation.ENV_VAR, raising=False)
    recreation.set_enabled(None)
    assert recreation.enabled() is True


@pytest.mark.parametrize(
    "value,expected",
    [("0", False), ("false", False), ("FALSE", False), ("no", False), ("off", False),
     (" off ", False),
     ("1", True), ("true", True), ("yes", True), ("on", True), ("maybe", True),
     # An empty value is a shell accident (`FOO= placeroot`), not a
     # considered "no", so it reads as the default rather than as off.
     ("", True), ("  ", True)],
)
def test_env_var_parsing(monkeypatch, value, expected):
    monkeypatch.setenv(recreation.ENV_VAR, value)
    recreation.set_enabled(None)
    assert recreation.enabled() is expected


def test_set_enabled_none_restores_env(monkeypatch):
    monkeypatch.setenv(recreation.ENV_VAR, "0")
    recreation.set_enabled(True)
    assert recreation.enabled() is True
    recreation.set_enabled(None)
    assert recreation.enabled() is False


# --- what the layer adds ---------------------------------------------------


def test_find_places_returns_base_theme_playgrounds(layer_on):
    rows = overture.find_places(CENTER_LAT, CENTER_LON, radius_m=1000,
                                category="playground", limit=25)
    assert _ids(rows) == {"lu-play-named", "lu-play-unnamed"}
    assert all(r["category"] == "playground" for r in rows)


def test_unnamed_rows_survive_with_a_null_name(layer_on):
    """The point of the layer: a nameless playground is still an answer.

    And nothing is invented to fill the gap — name is None, not "Playground".
    """
    rows = overture.find_places(CENTER_LAT, CENTER_LON, category="playground", limit=25)
    unnamed = [r for r in rows if r["id"] == "lu-play-unnamed"]
    assert len(unnamed) == 1
    assert unnamed[0]["name"] is None


def test_places_rows_still_need_a_name(layer_on, tmp_path):
    """The relaxed name filter applies to the layer only, not to Overture."""
    unnamed_place = tmp_path / "places_unnamed.parquet"
    con = duckdb.connect()
    con.execute(
        f"""COPY (
              SELECT * REPLACE ({{'primary': NULL}}::STRUCT("primary" VARCHAR) AS names)
              FROM read_parquet('{overture._upstream_glob()}')
            ) TO '{unnamed_place}' (FORMAT PARQUET)"""
    )
    con.close()
    overture.set_data_path(str(unnamed_place))
    try:
        rows = overture.find_places(CENTER_LAT, CENTER_LON, radius_m=2000, limit=25)
    finally:
        overture.set_data_path(None)
    assert all(r["id"].startswith(("lu-", "land-")) for r in rows), rows


def test_beaches_come_from_the_land_type(layer_on):
    rows = overture.find_places(CENTER_LAT, CENTER_LON, category="beach", limit=25)
    assert _ids(rows) == {"land-beach"}
    assert _names(rows) == {"Test Beach"}


def test_excluded_classes_are_not_unioned_in(layer_on):
    """pitch/garden/tree are in the fixtures and must never come back."""
    rows = overture.find_places(CENTER_LAT, CENTER_LON, radius_m=1000, limit=25)
    assert not _ids(rows) & {"lu-pitch", "lu-garden", "land-tree"}


def test_classes_collapse_onto_one_slug(layer_on):
    """state_park maps to nature_reserve — the slug, not the raw class."""
    rows = overture.find_places(CENTER_LAT, CENTER_LON, category="nature_reserve", limit=25)
    assert _ids(rows) == {"lu-reserve"}


def test_taxonomy_and_basic_category_come_from_the_bundled_csv(layer_on):
    """basic_category is the taxonomy root, so a branch search finds the row too."""
    rows = overture.find_places(CENTER_LAT, CENTER_LON, category="playground", limit=25)
    assert {r["basic_category"] for r in rows} == {"active_life"}
    by_branch = overture.find_places(CENTER_LAT, CENTER_LON, category="active_life", limit=25)
    assert {"lu-play-named", "lu-play-unnamed"} <= _ids(by_branch)


def test_distance_is_measured_from_the_polygon_centre(layer_on):
    """Not its south-west corner, which is what an un-collapsed bbox would give.

    lu-play-named spans lon CENTER+0.001..CENTER+0.003 and is centred on the
    query latitude, so its centre is 0.002 degrees of longitude east —
    ~169 m at this latitude — while its corner would read ~85 m.
    """
    (row,) = [
        r for r in overture.find_places(CENTER_LAT, CENTER_LON, category="playground", limit=25)
        if r["id"] == "lu-play-named"
    ]
    assert row["distance_m"] == pytest.approx(169, abs=8)
    assert row["lon"] == pytest.approx(CENTER_LON + 2 * _DEG, abs=1e-6)
    assert row["lat"] == pytest.approx(CENTER_LAT, abs=1e-6)


def test_radius_still_bounds_the_layer(layer_on):
    """A layer row outside the radius is dropped like any other row."""
    rows = overture.find_places(CENTER_LAT, CENTER_LON, radius_m=50,
                                category="playground", limit=25)
    assert "lu-play-named" not in _ids(rows)


def test_summarize_area_counts_the_layer(layer_on):
    without = None
    recreation.set_enabled(False)
    try:
        without = overture.summarize_area(CENTER_LAT, CENTER_LON, radius_m=1000)
    finally:
        recreation.set_enabled(True)
    with_layer = overture.summarize_area(CENTER_LAT, CENTER_LON, radius_m=1000)
    assert with_layer["total_places"] > without["total_places"]
    assert "active_life" in {c["category"] for c in with_layer["top_categories"]}


def test_find_places_in_bbox_includes_the_layer(layer_on):
    rows, _capped = overture.find_places_in_bbox(
        (CENTER_LON - 0.01, CENTER_LAT - 0.01, CENTER_LON + 0.01, CENTER_LAT + 0.01),
        category="playground", limit=25,
    )
    assert _ids(rows) == {"lu-play-named", "lu-play-unnamed"}


def test_place_details_resolves_a_base_theme_id(layer_on):
    row = overture.place_details(id="lu-park", near_lat=CENTER_LAT, near_lon=CENTER_LON)
    assert row["name"] == "Test Park"
    assert row["category"] == "park"
    # No listings fields exist for a base-theme polygon, and none are invented.
    assert row["confidence"] is None
    assert row["websites"] is None
    assert row["phones"] is None


def test_place_details_resolves_a_base_theme_id_without_a_hint(layer_on):
    """The unbounded id path unions the layer too (docs: place_details by id)."""
    assert overture.place_details(id="land-beach")["name"] == "Test Beach"


# --- the honest limitations, asserted rather than assumed -------------------


def test_min_confidence_excludes_the_layer(layer_on):
    """Documented in docs/RECREATION.md: these rows carry no confidence."""
    rows = overture.find_places(CENTER_LAT, CENTER_LON, category="playground",
                                min_confidence=0.1, limit=25)
    assert rows == []


def test_operating_status_excludes_the_layer(layer_on):
    rows = overture.find_places(CENTER_LAT, CENTER_LON, category="playground",
                                operating_status="in business", limit=25)
    assert rows == []


def test_a_drifted_base_schema_drops_that_branch_only(tmp_path, caplog):
    """No `class` column: the branch can't be built, and the places query
    still answers rather than failing over a supplementary layer."""
    land_use = tmp_path / "land_use.parquet"
    land = tmp_path / "land.parquet"
    _write_base_fixture(land_use, _LAND_USE_ROWS, include_class=False)
    _write_base_fixture(land, _LAND_ROWS)
    overture.set_data_path(str(land_use), theme="base", type_="land_use")
    overture.set_data_path(str(land), theme="base", type_="land")
    recreation.set_enabled(True)
    try:
        rows = overture.find_places(CENTER_LAT, CENTER_LON, radius_m=1000, limit=25)
        assert not _ids(rows) & {"lu-play-named", "lu-park"}
        # The healthy branch still contributes.
        assert _ids(overture.find_places(CENTER_LAT, CENTER_LON, category="beach")) == {
            "land-beach"
        }
        assert recreation.degraded_types() == ["land_use"]
    finally:
        recreation.set_enabled(None)
        overture.set_data_path(None, theme="base", type_="land_use")
        overture.set_data_path(None, theme="base", type_="land")


# --- provenance is visible -------------------------------------------------


def test_data_version_is_silent_when_the_layer_is_off():
    assert resources.recreation_payload() is None
    assert "recreation_layer" not in resources.data_version_payload()


def test_data_version_reports_the_layer(layer_on):
    payload = resources.data_version_payload()["recreation_layer"]
    assert payload["categories"] == recreation.CATEGORIES
    assert "land_use" in payload["source"] and "land" in payload["source"]
    assert recreation.ENV_VAR in payload["note"]
    assert "degraded_types" not in payload


def test_data_version_tool_carries_the_layer(layer_on):
    """Tool and resource share one payload, so both surfaces agree."""
    tool = server.data_version()
    assert tool["recreation_layer"] == resources.data_version_payload()["recreation_layer"]


# --- the cache stays honest ------------------------------------------------


def test_places_tiles_never_hold_base_theme_rows(layer_on):
    """A places tile is a faithful copy of theme=places, so switching the
    layer off can't leave its rows behind in the cache. The layer's own
    reads are keyed under land_use.py's existing base_<type> tiles."""
    assert recreation._cache_theme("land_use") == "base_land_use"
    assert recreation._cache_theme("land") == "base_land"
    source, active = overture._places_source((CENTER_LON - 0.01, CENTER_LAT - 0.01,
                                              CENTER_LON + 0.01, CENTER_LAT + 0.01))
    assert active is True
    # The places branch reads the places dataset and nothing else; the base
    # datasets appear only in the unioned branches after it.
    places_branch, _, rest = source.partition(" UNION ALL BY NAME ")
    assert "land_use.parquet" not in places_branch
    assert "land_use.parquet" in rest and "land.parquet" in rest


def test_every_mapped_slug_exists_in_the_bundled_taxonomy():
    """The map is only useful if find_places(category=slug) can reach it, and
    search_categories only returns slugs the bundled CSV knows."""
    from placeroot import categories

    for class_map in recreation.SOURCES.values():
        for class_, slug in class_map.items():
            assert categories.hierarchy_for(slug), f"{class_} -> {slug} not in the taxonomy"


def test_the_suite_pins_the_layer_off(monkeypatch):
    """Every test module other than this one queries places-only fixtures,
    so the autouse fixture in conftest pins the layer off — including for
    @live tests, which would otherwise pay for a second live scan."""
    assert recreation.enabled() is False


def test_gers_lookup_certifies_a_base_theme_id(layer_on):
    """The opposite of the rule the local supplement needed: these ids come
    from Overture's base theme and are real GERS ids, so gers_lookup should
    resolve one rather than refuse it."""
    from placeroot import gers

    row = gers.gers_lookup(id="lu-park", near_lat=CENTER_LAT, near_lon=CENTER_LON)
    assert row["theme"] == "places"
    assert row["name"] == "Test Park"


def test_name_search_sees_the_layer_like_find_places_does(layer_on):
    """resolve_place must be able to name a place find_places returns —
    otherwise the answer depends on which tool you asked."""
    from placeroot import geocode

    rows = geocode._query_places_fallback("Riverside Playground")
    assert "lu-play-named" in {r["id"] for r in rows}


# --- the layer follows the places dataset ----------------------------------


def test_a_places_only_pin_does_not_drag_the_base_theme_live(tmp_path, monkeypatch, caplog):
    """The shape examples/site_selection/run_demo.py --offline has, and the
    reason this rule exists: PLACEROOT_DATA_PATH pins places at a local file
    and says nothing about theme=base. Resolving base to the live release
    there would turn a deliberately-local query into a planet-scale remote
    scan, so the branch is skipped instead — with one warning naming the
    variable that would bring it back."""
    monkeypatch.setenv("PLACEROOT_DATA_PATH", str(tmp_path / "places.parquet"))
    recreation.set_enabled(True)
    recreation._pinned_warned.clear()
    try:
        with caplog.at_level("WARNING"):
            assert recreation.union_branches((-74.0, 40.0, -73.0, 41.0)) == []
        assert "PLACEROOT_DATA_PATH_BASE" in caplog.text
        assert recreation.degraded_types() == []
    finally:
        recreation.set_enabled(None)
        recreation._pinned_warned.clear()


def test_pinning_the_base_theme_too_brings_the_layer_back(layer_on):
    """Both pinned — what every other test in this module runs under — is the
    configuration where the layer is expected to read the local datasets."""
    assert overture.dataset_is_pinned("places", "place") is True
    assert overture.dataset_is_pinned("base", "land_use") is True
    assert len(recreation.union_branches((-74.0, 40.0, -73.0, 41.0))) == 2


def test_an_unpinned_deployment_is_unaffected(monkeypatch):
    """Nothing pinned at all — the ordinary live install — reads both themes
    from the release, which is the whole point of the layer."""
    monkeypatch.delenv("PLACEROOT_DATA_PATH", raising=False)
    overture.set_data_path(None)
    recreation.set_enabled(True)
    try:
        assert recreation._reaches_past_a_pinned_deployment("land_use") is False
    finally:
        recreation.set_enabled(None)
