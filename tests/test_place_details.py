"""Issue #9: one place in full, resolved by GERS id or by name + point."""

import duckdb
import pytest

from placeroot import overture, server

from .conftest import CENTER_LAT, CENTER_LON, FIXTURE_PATH, raw_rows


def _roastery_id() -> str:
    (row,) = [r for r in raw_rows() if r["name"] == "Blue Bottle Roastery"]
    return row["id"]


def test_resolve_by_id():
    result = overture.place_details(id=_roastery_id())
    assert result is not None
    assert result["name"] == "Blue Bottle Roastery"
    assert result["brand"] == "Blue Bottle Coffee"
    assert result["addresses"] == [
        {
            "freeform": "123 Main St", "locality": "Metropolis",
            "region": "NY", "postcode": "10001", "country": "US",
        },
    ]
    assert result["websites"] == ["https://bluebottleroastery.example"]
    assert result["phones"] == ["+1-555-0100"]
    assert result["socials"] == ["https://instagram.example/bluebottleroastery"]
    assert result["sources"] == [{"dataset": "meta", "record_id": "meta-001"}]
    assert "confidence" in result
    assert "operating_status" in result


def test_resolve_by_name_and_point():
    result = overture.place_details(name="Roastery", lat=CENTER_LAT, lon=CENTER_LON, radius_m=1000)
    assert result is not None
    assert result["id"] == _roastery_id()


def test_resolve_by_name_and_point_nearest_wins():
    """Several "Cluster Place NNN" names exist; a substring match near a
    specific point must resolve to the nearest one, not an arbitrary one."""
    result = overture.place_details(
        name="Cluster Place", lat=CENTER_LAT, lon=CENTER_LON, radius_m=50
    )
    assert result is not None
    nearby_ids = {
        r["id"] for r in overture.find_places(
            CENTER_LAT, CENTER_LON, radius_m=50, name="Cluster Place", limit=25
        )
    }
    assert result["id"] in nearby_ids


def test_requires_id_or_name_and_point():
    with pytest.raises(ValueError):
        overture.place_details()
    with pytest.raises(ValueError):
        overture.place_details(name="Roastery")


def test_not_found_by_id_returns_none():
    assert overture.place_details(id="does-not-exist") is None


def test_not_found_by_name_and_point_returns_none():
    assert overture.place_details(name="Nonexistent Place XYZ", lat=0.0, lon=0.0) is None


def test_long_arrays_truncate_with_a_count_never_silently():
    """Regression for #9: the "Cluster Place 010" fixture row has 8
    addresses and 8 sources — more than the truncation cap."""
    result = overture.place_details(
        name="Cluster Place 010", lat=CENTER_LAT, lon=CENTER_LON, radius_m=1000
    )
    assert result is not None
    assert len(result["addresses"]) < 8
    assert result["addresses_omitted_count"] == 8 - len(result["addresses"])
    assert len(result["sources"]) < 8
    assert result["sources_omitted_count"] == 8 - len(result["sources"])


def test_confidence_missing_degrades_gracefully(tmp_path):
    out = tmp_path / "missing_confidence.parquet"
    con = duckdb.connect()
    con.execute(
        "COPY (SELECT * EXCLUDE (confidence) FROM read_parquet("
        f"'{FIXTURE_PATH}')) TO '{out}' (FORMAT PARQUET)"
    )
    overture.set_data_path(str(out))
    result = overture.place_details(id=_roastery_id())
    assert result is not None
    assert result["confidence"] is None


def test_server_wraps_not_found():
    result = server.place_details(id="does-not-exist")
    assert result == {
        "error": "not_found",
        "detail": "no place matched id, or name near lat/lon",
    }


def test_server_wraps_bad_request():
    result = server.place_details()
    assert result["error"] == "bad_request"


def test_server_happy_path_includes_id():
    result = server.place_details(id=_roastery_id())
    assert "error" not in result
    assert result["id"] == _roastery_id()


def test_server_structured_error_on_unreachable_upstream(tmp_path):
    overture.set_data_path(str(tmp_path / "does-not-exist" / "*.parquet"))
    result = server.place_details(id="whatever")
    assert result["error"] == "upstream_unavailable"
