"""Issue #348: polygon -> overlapping divisions, against the synthetic
division_area fixture (tests/fixtures/division_areas.parquet) — the same
nested rectangles admin_lookup uses around places.parquet's downtown
cluster (Downtown/neighborhood inside Metropolis/locality), plus one
unrelated polygon ("Arctica") that must never appear."""

import duckdb
import pytest

from placeroot import divisions, overture

from .conftest import DIVISION_AREAS_FIXTURE_PATH

# Downtown/neighborhood: POLYGON((-73.905 40.695, -73.905 40.705,
# -73.895 40.705, -73.895 40.695, ...)). This query polygon fully contains
# it and also overlaps (partially) Metropolis/locality
# (-74 40.6, -74 40.8, -73.8 40.8, -73.8 40.6).
DOWNTOWN_QUERY_POLYGON = {
    "type": "Polygon",
    "coordinates": [
        [
            [-73.91, 40.69],
            [-73.91, 40.71],
            [-73.89, 40.71],
            [-73.89, 40.69],
            [-73.91, 40.69],
        ]
    ],
}

# Nowhere near any fixture polygon.
OCEAN_QUERY_POLYGON = {
    "type": "Polygon",
    "coordinates": [
        [
            [-30.0, 0.0],
            [-30.0, 1.0],
            [-29.0, 1.0],
            [-29.0, 0.0],
            [-30.0, 0.0],
        ]
    ],
}


def test_downtown_polygon_returns_neighborhood_and_locality():
    result = divisions.divisions_in_polygon(DOWNTOWN_QUERY_POLYGON)
    by_subtype = {r["subtype"]: r for r in result["results"]}
    assert "neighborhood" in by_subtype
    assert "locality" in by_subtype
    for r in result["results"]:
        assert 0 < r["overlap_fraction"] <= 1.0
    # The query polygon fully contains Downtown, so Downtown's own area is
    # entirely inside it.
    assert by_subtype["neighborhood"]["overlap_fraction"] == pytest.approx(1.0, abs=1e-3)
    assert by_subtype["neighborhood"]["name"] == "Downtown"


def test_subtypes_filter_excludes_other_subtypes():
    result = divisions.divisions_in_polygon(DOWNTOWN_QUERY_POLYGON, subtypes=("neighborhood",))
    subtypes = {r["subtype"] for r in result["results"]}
    assert subtypes == {"neighborhood"}


def test_results_ranked_by_overlap_fraction_descending():
    result = divisions.divisions_in_polygon(DOWNTOWN_QUERY_POLYGON)
    fractions = [r["overlap_fraction"] for r in result["results"]]
    assert fractions == sorted(fractions, reverse=True)


def test_ocean_polygon_returns_empty_results():
    result = divisions.divisions_in_polygon(OCEAN_QUERY_POLYGON)
    assert result == {"results": []}


def test_arctica_never_appears_for_downtown_polygon():
    result = divisions.divisions_in_polygon(DOWNTOWN_QUERY_POLYGON)
    names = {r["name"] for r in result["results"]}
    assert "Arctica" not in names


def test_missing_geometry_raises_schema_degraded(tmp_path):
    out = tmp_path / "missing_geometry.parquet"
    con = duckdb.connect()
    con.execute(
        "COPY (SELECT * EXCLUDE (geometry) FROM read_parquet("
        f"'{DIVISION_AREAS_FIXTURE_PATH}')) TO '{out}' (FORMAT PARQUET)"
    )
    overture.set_data_path(str(out), theme="divisions")
    with pytest.raises(overture.SchemaDegraded) as exc_info:
        divisions.divisions_in_polygon(DOWNTOWN_QUERY_POLYGON)
    assert "geometry" in exc_info.value.missing


def test_missing_subtype_raises_schema_degraded(tmp_path):
    out = tmp_path / "missing_subtype.parquet"
    con = duckdb.connect()
    con.execute(
        "COPY (SELECT * EXCLUDE (subtype) FROM read_parquet("
        f"'{DIVISION_AREAS_FIXTURE_PATH}')) TO '{out}' (FORMAT PARQUET)"
    )
    overture.set_data_path(str(out), theme="divisions")
    with pytest.raises(overture.SchemaDegraded) as exc_info:
        divisions.divisions_in_polygon(DOWNTOWN_QUERY_POLYGON)
    assert "subtype" in exc_info.value.missing


def test_non_polygon_input_raises_value_error():
    with pytest.raises(ValueError):
        divisions.divisions_in_polygon({"type": "Point", "coordinates": [-73.9, 40.7]})
    with pytest.raises(ValueError):
        divisions.divisions_in_polygon({"type": "Polygon", "coordinates": []})
    with pytest.raises(ValueError):
        divisions.divisions_in_polygon("not a dict")


def test_malformed_ring_nesting_raises_clean_value_error():
    # Ring nesting omitted: coordinates is a list of points, not of rings.
    with pytest.raises(ValueError, match="polygon_geojson"):
        divisions.divisions_in_polygon(
            {"type": "Polygon", "coordinates": [[-73.9, 40.7], [-73.8, 40.7]]}
        )
    # Non-numeric vertex.
    with pytest.raises(ValueError, match="polygon_geojson"):
        divisions.divisions_in_polygon(
            {"type": "Polygon", "coordinates": [[["a", "b"], [-73.8, 40.7]]]}
        )
    # Empty ring.
    with pytest.raises(ValueError, match="polygon_geojson"):
        divisions.divisions_in_polygon({"type": "Polygon", "coordinates": [[]]})


def test_continent_sized_polygon_raises_value_error():
    huge = {
        "type": "Polygon",
        "coordinates": [
            [[-120.0, 20.0], [-120.0, 60.0], [-60.0, 60.0], [-60.0, 20.0], [-120.0, 20.0]]
        ],
    }
    with pytest.raises(ValueError, match="maximum supported extent"):
        divisions.divisions_in_polygon(huge)


def test_antimeridian_spanning_polygon_mentions_antimeridian():
    # Naively encoded Fiji-ish box: lon jumps from +179 to -179, giving a
    # 358-degree bbox span.
    seam = {
        "type": "Polygon",
        "coordinates": [
            [[179.0, -17.0], [179.0, -16.0], [-179.0, -16.0], [-179.0, -17.0], [179.0, -17.0]]
        ],
    }
    with pytest.raises(ValueError, match="antimeridian"):
        divisions.divisions_in_polygon(seam)


def test_limit_is_clamped_to_max_rows():
    result = divisions.divisions_in_polygon(DOWNTOWN_QUERY_POLYGON, limit=10_000)
    assert len(result["results"]) <= overture.MAX_ROWS


def test_bbox_less_schema_still_answers(tmp_path):
    # Without the bbox column the prefilter (and its named parameters) must
    # both drop out — DuckDB rejects named parameters the SQL never uses.
    out = tmp_path / "missing_bbox.parquet"
    con = duckdb.connect()
    con.execute(
        "COPY (SELECT * EXCLUDE (bbox) FROM read_parquet("
        f"'{DIVISION_AREAS_FIXTURE_PATH}')) TO '{out}' (FORMAT PARQUET)"
    )
    overture.set_data_path(str(out), theme="divisions")
    result = divisions.divisions_in_polygon(DOWNTOWN_QUERY_POLYGON)
    assert {r["name"] for r in result["results"]} >= {"Downtown"}


def test_tiny_real_overlap_is_reported_nonzero():
    # A sliver over Metropolis's west edge: overlap is ~1e-4 of its area,
    # which rounds to 0.0 at 3 decimals — the fraction must stay positive.
    sliver = {
        "type": "Polygon",
        "coordinates": [
            [
                [-74.005, 40.700],
                [-74.005, 40.701],
                [-73.995, 40.701],
                [-73.995, 40.700],
                [-74.005, 40.700],
            ]
        ],
    }
    result = divisions.divisions_in_polygon(sliver, subtypes=("locality",))
    assert result["results"]
    for r in result["results"]:
        assert r["overlap_fraction"] > 0


def test_zero_limit_raises_value_error():
    with pytest.raises(ValueError):
        divisions.divisions_in_polygon(DOWNTOWN_QUERY_POLYGON, limit=0)


def test_every_result_carries_an_id():
    result = divisions.divisions_in_polygon(DOWNTOWN_QUERY_POLYGON)
    assert result["results"]
    assert all(r["id"] for r in result["results"])
