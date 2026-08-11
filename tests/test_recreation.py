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
    """The unbounded id path unions the layer too (docs: place_details by id).

    This works offline because the base datasets are pinned local files; an
    unpinned deployment's unbounded lookup serves from cached tiles only —
    see test_an_unbounded_lookup_never_scans_the_live_base_theme.
    """
    assert overture.place_details(id="land-beach")["name"] == "Test Beach"


def test_place_details_labels_the_source_theme(layer_on):
    """A recreation-layer row says where it came from; places rows carry no
    marker at all (their theme is the tool's default)."""
    base_row = overture.place_details(id="lu-park", near_lat=CENTER_LAT, near_lon=CENTER_LON)
    assert base_row["source_theme"] == "base"
    places_id = overture.find_places(CENTER_LAT, CENTER_LON, radius_m=2000, limit=25)
    places_id = [r["id"] for r in places_id if not r["id"].startswith(("lu-", "land-"))][0]
    places_row = overture.place_details(id=places_id, near_lat=CENTER_LAT, near_lon=CENTER_LON)
    assert "source_theme" not in places_row


def test_an_unbounded_lookup_never_scans_the_live_base_theme():
    """bbox None with nothing pinned and nothing cached: the branch is
    skipped (returns None), not resolved to a planet-scale S3 glob — the
    place_details/geocode regression the review found."""
    recreation.set_enabled(True)
    try:
        assert overture.dataset_is_pinned("base", "land_use") is False
        assert recreation._from_source(None, "land_use") is None
    finally:
        recreation.set_enabled(None)


def test_an_unreadable_base_dataset_drops_the_branch(layer_on, tmp_path, caplog):
    """What a partial mirror looks like: the glob resolves but nothing is
    there. Before the probe check this passed 'assume nothing missing' and
    failed every places query at scan time; now the branch drops, the query
    answers, and data_version names the gap."""
    overture.set_data_path(str(tmp_path / "does_not_exist.parquet"),
                           theme="base", type_="land_use")
    try:
        with caplog.at_level("WARNING"):
            rows = overture.find_places(CENTER_LAT, CENTER_LON, radius_m=1000, limit=25)
        assert not _ids(rows) & {"lu-play-named", "lu-park"}
        assert "unreadable" in caplog.text
        # The healthy branch still contributes, and the gap is reported.
        assert _ids(overture.find_places(CENTER_LAT, CENTER_LON, category="beach")) == {
            "land-beach"
        }
        assert recreation.degraded_types() == ["land_use"]
    finally:
        overture.set_data_path(str(layer_on / "land_use.parquet"),
                               theme="base", type_="land_use")


# --- one real-world place, one row ------------------------------------------


# The duplicate places playground sits ~28 m from lu-play-unnamed's centre
# (same real place, so the places row must win) and ~167 m from
# lu-play-named's centre (a distinct playground past DEDUP_RADIUS_M, kept).
_DUP_LAT = CENTER_LAT + 1.2 * _DEG
_DUP_LON = CENTER_LON + 0.8 * _DEG


def _write_places_fixture_plus(path, extra_rows) -> None:
    """The committed places fixture plus extra_rows: (id, name, slug, lat, lon).

    Each extra row clones a fixture row's remaining columns, so the shape
    stays exactly the committed one.
    """
    con = duckdb.connect()
    con.execute(f"CREATE TABLE p AS SELECT * FROM read_parquet('{overture._upstream_glob()}')")
    for id_, name, slug, lat, lon in extra_rows:
        con.execute(f"""
            INSERT INTO p
            SELECT * REPLACE (
                '{id_}' AS id,
                {{'primary': '{name}'}} AS names,
                {{'primary': '{slug}', 'alternates': []}} AS taxonomy,
                'active_life' AS basic_category,
                {{'xmin': {lon}, 'ymin': {lat}, 'xmax': {lon}, 'ymax': {lat}}} AS bbox
            )
            FROM p LIMIT 1
        """)
    con.execute(f"COPY p TO '{path}' (FORMAT PARQUET)")
    con.close()


@pytest.fixture
def places_with_duplicate_playground(tmp_path):
    """The committed places fixture plus one playground row that duplicates
    a base-theme playground — the both-themes overlap dedup exists for."""
    dup = tmp_path / "places_dup.parquet"
    _write_places_fixture_plus(
        dup, [("places-dup-playground", "Fixture Playground", "playground",
               _DUP_LAT, _DUP_LON)]
    )
    overture.set_data_path(str(dup))
    try:
        yield dup
    finally:
        overture.set_data_path(None)


def test_a_place_in_both_themes_returns_once(layer_on, places_with_duplicate_playground):
    """The places row wins (it is the richer one); a base row past
    DEDUP_RADIUS_M is a distinct real-world place and survives."""
    rows = overture.find_places(CENTER_LAT, CENTER_LON, radius_m=1000,
                                category="playground", limit=25)
    ids = _ids(rows)
    assert "places-dup-playground" in ids
    assert "lu-play-unnamed" not in ids
    assert "lu-play-named" in ids


def test_bbox_queries_dedup_too(layer_on, places_with_duplicate_playground):
    rows, _capped = overture.find_places_in_bbox(
        (CENTER_LON - 0.01, CENTER_LAT - 0.01, CENTER_LON + 0.01, CENTER_LAT + 0.01),
        category="playground", limit=25,
    )
    assert "lu-play-unnamed" not in _ids(rows)
    assert {"places-dup-playground", "lu-play-named"} <= _ids(rows)


def test_summarize_area_counts_a_duplicated_place_once(layer_on, tmp_path):
    """Adding a places listing for a place the base theme already carries
    must not raise the area's total: the base row stops being counted the
    moment the richer places row exists."""
    before = overture.summarize_area(CENTER_LAT, CENTER_LON, radius_m=1000)["total_places"]
    dup = tmp_path / "places_dup.parquet"
    _write_places_fixture_plus(
        dup, [("places-dup-playground", "Fixture Playground", "playground",
               _DUP_LAT, _DUP_LON)]
    )
    overture.set_data_path(str(dup))
    try:
        after = overture.summarize_area(CENTER_LAT, CENTER_LON, radius_m=1000)["total_places"]
    finally:
        overture.set_data_path(None)
    assert after == before


def test_place_details_by_name_prefers_the_places_row(layer_on, tmp_path):
    """Nearest-name-match must not surface the sparse base polygon when the
    same real place has a places listing a few meters further away.

    The listing here sits ~30 m from lu-play-named's centre — same real
    playground — so the name search from the polygon's own centre must
    return the places row even though the base row is nearer.
    """
    dup = tmp_path / "places_dup_named.parquet"
    near_lat, near_lon = CENTER_LAT + 0.2 * _DEG, CENTER_LON + 2.2 * _DEG
    _write_places_fixture_plus(
        dup, [("places-dup-named", "Riverside Playground", "playground",
               near_lat, near_lon)]
    )
    overture.set_data_path(str(dup))
    try:
        row = overture.place_details(
            name="Riverside Playground", lat=CENTER_LAT, lon=CENTER_LON + 2 * _DEG,
            radius_m=200,
        )
    finally:
        overture.set_data_path(None)
    assert row["id"] == "places-dup-named"
    assert "source_theme" not in row


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
    resolve one rather than refuse it — and label the theme that actually
    owns it, not the places theme it happened to resolve through."""
    from placeroot import gers

    row = gers.gers_lookup(id="lu-park", near_lat=CENTER_LAT, near_lon=CENTER_LON)
    assert row["theme"] == "base"
    assert row["type"] == "land_use"
    assert row["name"] == "Test Park"


def test_gers_lookup_types_a_beach_as_land(layer_on):
    from placeroot import gers

    row = gers.gers_lookup(id="land-beach", near_lat=CENTER_LAT, near_lon=CENTER_LON)
    assert (row["theme"], row["type"]) == ("base", "land")


def test_gers_lookup_still_labels_places_rows_as_places(layer_on):
    from placeroot import gers

    rows = overture.find_places(CENTER_LAT, CENTER_LON, radius_m=2000, limit=1)
    places_rows = [r for r in rows if not r["id"].startswith(("lu-", "land-"))]
    if not places_rows:
        pytest.skip("fixture returned no places rows in range")
    row = gers.gers_lookup(id=places_rows[0]["id"], near_lat=CENTER_LAT, near_lon=CENTER_LON)
    assert (row["theme"], row["type"]) == ("places", "place")


def test_name_search_sees_the_layer_like_find_places_does(layer_on):
    """resolve_place must be able to name a place find_places returns —
    otherwise the answer depends on which tool you asked."""
    from placeroot import geocode

    rows = geocode._query_places_fallback("Riverside Playground")
    assert "lu-play-named" in {r["id"] for r in rows}


def test_an_anchored_name_search_bounds_the_layer_too(layer_on):
    """The anchor bbox issue #83 builds for the places scan is passed through
    to the layer's own reads instead of being discarded (review finding)."""
    from placeroot import geocode

    rows = geocode._query_places_fallback("Riverside Playground",
                                          anchor=(CENTER_LAT, CENTER_LON))
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
        # data_version must not advertise a layer that contributes nothing:
        # both skipped types are reported as degraded (see recreation_payload).
        assert recreation.degraded_types() == ["land_use", "land"]
        assert resources.recreation_payload()["degraded_types"] == ["land_use", "land"]
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


# --- the default-on path against the real datasets ---------------------------


@pytest.mark.live
def test_live_union_of_the_two_real_themes():
    """The one configuration the synthetic fixtures cannot vouch for: real
    theme=places unioned with real theme=base. The two themes version their
    struct columns (names, sources, bbox) independently; if they drift
    apart, UNION ALL BY NAME fails outright and every places query in a
    default-on deployment breaks — this is the test that sees it first.
    """
    recreation.set_enabled(True)
    try:
        rows = overture.find_places(40.7359, -73.9911, radius_m=500,
                                    category="playground", limit=10)
    finally:
        recreation.set_enabled(False)
    assert rows, "no playgrounds within 500 m of Union Square is itself a red flag"
